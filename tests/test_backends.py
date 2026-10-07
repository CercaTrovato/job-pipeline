from __future__ import annotations

import json
import pathlib
import types

import pytest

from jp import backends, packets, prompts

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "llm" / "schemas" / "analyze.v1.json"
GOOD = json.loads((ROOT / "tests" / "fixtures" / "jd" / "analyze_good.json").read_text(encoding="utf-8"))


def _api(tmp_path, monkeypatch):
    monkeypatch.setenv("JP_LLM_API_KEY", "sk-from-env")
    return backends.load_api_config({"llm": {"api": {
        "base_url": "https://relay.example/v1", "model": "explicit-model", "protocol": "responses",
        "credential": {"kind": "env", "name": "JP_LLM_API_KEY"},
        "reasoning_effort": "low", "concurrency": 2, "timeout_sec": 5,
    }}})


def test_api_config_requires_explicit_values_and_ignores_codex_auth(tmp_path, monkeypatch):
    codex_home = tmp_path / "codexhome"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "private-model"\n', encoding="utf-8")
    (codex_home / "auth.json").write_text('{"OPENAI_API_KEY":"private-secret"}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("JP_LLM_API_KEY", raising=False)
    original = pathlib.Path.read_text

    def guarded_read(path, *args, **kwargs):
        if path.is_relative_to(codex_home):
            pytest.fail("API config must not read Codex configuration or auth files")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", guarded_read)
    explicit = {"llm": {"api": {"base_url": "https://x/v1", "model": "m", "protocol": "responses",
                                  "credential": {"kind": "env", "name": "JP_LLM_API_KEY"}}}}
    with pytest.raises(RuntimeError, match="凭据"):
        backends.load_api_config(explicit)
    monkeypatch.setenv("JP_LLM_API_KEY", "sk-explicit")
    api = backends.load_api_config(explicit)
    assert (api.base_url, api.model, api.protocol, api.key) == ("https://x/v1", "m", "responses", "sk-explicit")


def test_call_api_parses_responses_output_with_official_sdk_shape(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    seen = {}

    class Client:
        def __init__(self, **kwargs):
            seen["client"] = kwargs
            self.responses = types.SimpleNamespace(create=self.create)

        def create(self, **kwargs):
            seen["request"] = kwargs
            return types.SimpleNamespace(output_text=json.dumps(GOOD, ensure_ascii=False), usage=None)

    monkeypatch.setitem(__import__("sys").modules, "openai", types.SimpleNamespace(OpenAI=Client))
    out = backends.call_api("PROMPT", json.loads(SCHEMA.read_text(encoding="utf-8")), api, "analyze_v1")
    assert out == GOOD
    assert seen["client"]["api_key"] == "sk-from-env"
    assert seen["request"]["text"]["format"]["type"] == "json_schema"
    assert seen["request"]["input"][0]["content"] == "PROMPT"


def test_run_api_writes_output_error_and_retries_once(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    jd = "熟悉 Python"
    p1 = packets.create(tmp_path / "t", "j1", "analyze", {"jd_text": jd}, "P1", SCHEMA)
    p2 = packets.create(tmp_path / "t", "j2", "analyze", {"jd_text": jd}, "P2", SCHEMA)
    calls = []

    def fake_call(prompt, schema, api, schema_name):
        calls.append(prompt)
        if prompt == "P2":
            raise RuntimeError("boom sk-from-env")
        return GOOD

    monkeypatch.setattr(backends, "call_api", fake_call)
    done = []
    result = backends.run_api([p1, p2], api, on_done=lambda: done.append(1))
    assert (result.ok, result.failed, len(done)) == (1, 1, 2)
    assert packets.status(p1) == "done" and packets.validate(p1) == GOOD
    assert packets.status(p2) == "error"
    assert not (p2.dir / "output.json").exists()
    err = (p2.dir / "error.txt").read_text(encoding="utf-8")
    assert "boom" in err and "sk-from-env" not in err
    assert calls.count("P2") == 2


def test_failed_final_quote_validation_removes_output(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    packet = packets.create(tmp_path / "t", "job", "analyze", {"jd_text": "熟悉 Python"}, "P", SCHEMA)
    invalid = dict(GOOD)
    invalid["atomic_requirements"] = [dict(GOOD["atomic_requirements"][0], quote="JD中不存在")]
    monkeypatch.setattr(backends, "call_api", lambda *args: invalid)
    result = backends.run_api([packet], api)
    assert (result.ok, result.failed) == (0, 1)
    assert packets.status(packet) == "error" and not (packet.dir / "output.json").exists()


def test_codex_command_has_no_default_model_but_accepts_explicit_model(tmp_path):
    packet = packets.create(tmp_path / "t", "j1", "analyze", {"a": 1}, "P", SCHEMA)
    cmd = backends.codex_command(packet, reasoning_effort="low")
    assert "-m" not in cmd and str(packet.dir) in cmd
    cmd = backends.codex_command(packet, reasoning_effort="low", model="user-selected-model")
    assert cmd[cmd.index("-m") + 1] == "user-selected-model"
    assert "--ignore-user-config" in cmd
    assert cmd[cmd.index("--output-schema") + 1] == str(packet.dir / "schema.json")
    assert cmd[cmd.index("-o") + 1] == str(packet.dir / "output.json") and cmd[-1] == "-"
    assert "model_reasoning_effort=low" in cmd


def test_codex_prompt_removes_manual_file_instructions():
    prompt = "方法与输入\n## 输出要求\n- 把结果写到 output.json"
    assert prompts.for_codex(prompt) == "方法与输入"


def test_run_codex_validates_and_redacts_cli_stderr(tmp_path, monkeypatch):
    p1 = packets.create(tmp_path / "t", "j1", "analyze", {"jd_text": "熟悉 Python"}, "P1", SCHEMA)
    p2 = packets.create(tmp_path / "t", "j2", "analyze", {"jd_text": "熟悉 Python"}, "P2", SCHEMA)
    calls = []

    def fake_run(cmd, input, timeout, capture_output):
        calls.append((cmd, input))
        if input == b"P1":
            (p1.dir / "output.json").write_text(json.dumps(GOOD), encoding="utf-8")
            event = b'{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":20,"output_tokens":30}}'
            return type("Result", (), {"returncode": 0, "stderr": b"", "stdout": event})()
        return type("Result", (), {"returncode": 1, "stderr": b"secret-key", "stdout": b""})()

    monkeypatch.setattr(backends.subprocess, "run", fake_run)
    ticks = []
    result = backends.run_codex([p1, p2], retries=1, concurrency=2, model="test-model",
                                on_done=lambda: ticks.append(1))
    assert (result.ok, result.failed, len(ticks)) == (1, 1, 2)
    assert packets.validate(p1) == GOOD
    assert len([call for call in calls if call[1] == b"P2"]) == 2
    assert packets.status(p2) == "error"
    assert "secret-key" not in (p2.dir / "error.txt").read_text(encoding="utf-8")
    assert (result.input_tokens, result.cached_input_tokens, result.output_tokens, result.usage_reported) == (100, 20, 30, 1)


def test_codex_ground_quotes_only_from_jd(tmp_path):
    jd = "支持任务动作路径轨迹规划及控制的算法研究与工程落地"
    packet = packets.create(tmp_path / "t", "j1", "analyze", {"jd_text": jd}, "P", SCHEMA)
    out = dict(GOOD)
    out["atomic_requirements"] = [dict(GOOD["atomic_requirements"][0], quote="支持任务动作路径轨迹规划及控制的算法研究及工程落地")]
    backends._ground_analyze_quotes(packet, out)
    quote = out["atomic_requirements"][0]["quote"]
    assert quote in jd and len(quote) >= 8
    assert (packet.dir / "quote_repairs.json").exists()
    out["atomic_requirements"][0]["quote"] = "完全不存在"
    with pytest.raises(packets.PacketError):
        backends._ground_analyze_quotes(packet, out)


def test_missing_prompt_file_becomes_error_txt(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    packet = packets.create(tmp_path / "t", "j1", "analyze", {"jd_text": "熟悉 Python"}, "P", SCHEMA)
    (packet.dir / "prompt.md").unlink()
    result = backends.run_api([packet], api)
    assert (result.ok, result.failed) == (0, 1) and packets.status(packet) == "error"
    assert "sk-from-env" not in (packet.dir / "error.txt").read_text(encoding="utf-8")


def test_api_config_repr_hides_key(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    assert "sk-from-env" not in repr(api) and "sk-from-env" not in str(api)
