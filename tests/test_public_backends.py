from __future__ import annotations

import json
import pathlib
import sys
import types

import pytest

from jp import backends, packets


OUTPUT = {"ok": True}
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}


def _config(protocol="responses", key="secret-token-that-must-not-leak"):
    return {"llm": {"backend": "api", "api": {
        "base_url": "https://api.example/v1", "model": "provider-model", "protocol": protocol,
        "credential": {"kind": "env", "name": "JP_LLM_API_KEY"},
        "concurrency": 1, "timeout_sec": 5, "reasoning_effort": "low",
    }}}


def _response(protocol, text, usage=None):
    usage = usage or types.SimpleNamespace(input_tokens=11, output_tokens=3,
                                           input_tokens_details=types.SimpleNamespace(cached_tokens=2),
                                           cache_read_input_tokens=0)
    if protocol == "responses":
        return types.SimpleNamespace(output_text=text, usage=usage)
    if protocol == "chat_completions":
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text))],
                                     usage=usage)
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], usage=usage)


def _mock_sdk(monkeypatch, protocol, response, seen):
    class Client:
        def __init__(self, **kwargs):
            seen["client"] = kwargs
            def respond(**kw):
                seen["calls"] = seen.get("calls", 0) + 1
                seen["request"] = kw
                return response
            if protocol == "responses":
                self.responses = types.SimpleNamespace(create=respond)
            elif protocol == "chat_completions":
                self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=respond))
            else:
                self.messages = types.SimpleNamespace(create=respond)
    module = "anthropic" if protocol == "anthropic" else "openai"
    ctor = "Anthropic" if protocol == "anthropic" else "OpenAI"
    monkeypatch.setitem(sys.modules, module, types.SimpleNamespace(**{ctor: Client}))


@pytest.mark.parametrize("protocol", ["responses", "chat_completions", "anthropic"])
def test_protocol_sdk_calls_and_json_parse(monkeypatch, protocol):
    seen = {}
    _mock_sdk(monkeypatch, protocol, _response(protocol, json.dumps(OUTPUT)), seen)
    monkeypatch.setenv("JP_LLM_API_KEY", "secret-token-that-must-not-leak")
    api = backends.load_api_config(_config(protocol))
    assert backends.call_api("fictional prompt", SCHEMA, api, "sample_v1") == OUTPUT
    assert seen["client"]["api_key"] == "secret-token-that-must-not-leak"
    assert seen["client"]["base_url"] == "https://api.example/v1"
    request = seen["request"]
    if protocol == "responses":
        assert request["text"]["format"]["schema"] == SCHEMA
    elif protocol == "chat_completions":
        assert request["response_format"] == {"type": "json_object"}
        system = request["messages"][0]["content"]
        assert "Output valid json only" in system and json.dumps(SCHEMA, separators=(",", ":")) in system
        assert "JSON example:" in system
    else:
        assert request["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
        assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "secret-token-that-must-not-leak" not in repr(api)


def test_api_configuration_requires_explicit_endpoint_model_protocol_and_key(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unrelated-codex"))
    (tmp_path / "unrelated-codex").mkdir()
    (tmp_path / "unrelated-codex" / "config.toml").write_text('model = "private-model"\n', encoding="utf-8")
    (tmp_path / "unrelated-codex" / "auth.json").write_text('{"OPENAI_API_KEY":"private-secret"}', encoding="utf-8")
    monkeypatch.delenv("JP_LLM_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="base_url"):
        backends.load_api_config({"llm": {"api": {"model": "configured"}}})
    with pytest.raises(RuntimeError, match="protocol"):
        backends.load_api_config({"llm": {"api": {"base_url": "https://example", "model": "configured"}}})
    cfg = _config()
    cfg["llm"]["api"].pop("credential")
    with pytest.raises(RuntimeError, match="凭据"):
        backends.load_api_config(cfg)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_4xx_is_not_retried_and_credentials_are_scrubbed(monkeypatch, tmp_path, status):
    key = "secret-token-that-must-not-leak"
    monkeypatch.setenv("JP_LLM_API_KEY", key)
    api = backends.load_api_config(_config())
    p = packets.create(tmp_path / "tasks", "job", "profile", {"fictional": True}, "prompt", _schema_file(tmp_path))
    calls = []

    class FakeError(Exception):
        status_code = status

    class Client:
        def __init__(self, **kwargs):
            self.responses = types.SimpleNamespace(create=self.call)

        def call(self, **kwargs):
            calls.append(kwargs)
            raise FakeError("request failed with " + key)

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=Client))
    result = backends.run_api([p], api)
    assert (result.ok, result.failed, len(calls)) == (0, 1, 1)
    content = (p.dir / "error.txt").read_text(encoding="utf-8")
    assert key not in content
    assert key not in (p.dir / "usage.json").read_text(encoding="utf-8")


def _schema_file(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(SCHEMA), encoding="utf-8")
    return path


def test_invalid_json_retries_once_and_usage_is_written(monkeypatch, tmp_path):
    monkeypatch.setenv("JP_LLM_API_KEY", "secret-token-that-must-not-leak")
    api = backends.load_api_config(_config())
    p = packets.create(tmp_path / "tasks", "job", "profile", {"fictional": True}, "prompt", _schema_file(tmp_path))
    responses = [_response("responses", "not json"), _response("responses", json.dumps(OUTPUT))]
    calls = []

    class Client:
        def __init__(self, **kwargs):
            self.responses = types.SimpleNamespace(create=lambda **kw: (calls.append(kw) or responses.pop(0)))

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=Client))
    result = backends.run_api([p], api)
    assert (result.ok, result.failed, len(calls)) == (1, 0, 2)
    assert packets.validate(p) == OUTPUT
    usage = json.loads((p.dir / "usage.json").read_text(encoding="utf-8"))
    assert usage["input_tokens"] == 22 and usage["cached_input_tokens"] == 4
    assert "secret-token-that-must-not-leak" not in json.dumps(usage)


@pytest.mark.parametrize("protocol", ["responses", "chat_completions", "anthropic"])
def test_diagnostics_model_flag_makes_one_synthetic_sdk_call(monkeypatch, tmp_path, protocol):
    from jp import diagnostics

    seen = {}
    _mock_sdk(monkeypatch, protocol, _response(protocol, json.dumps(OUTPUT)), seen)
    monkeypatch.setenv("JP_LLM_API_KEY", "secret-token-that-must-not-leak")
    cfg = _config(protocol)
    cfg["paths"] = {"tasks_dir": str(tmp_path / "workspace" / "tasks")}
    result = diagnostics.check(cfg, test_model=True)
    assert result["status"] == "configured" and result["tested"] is True
    assert seen["calls"] == 1
    assert "secret-token-that-must-not-leak" not in json.dumps(result)


def test_configure_test_model_accepts_success_and_preserves_config_on_failure(monkeypatch, tmp_path):
    from jp import diagnostics, settings

    monkeypatch.setenv("JP_LLM_API_KEY", "secret-token-that-must-not-leak")
    ws = settings.Workspace(tmp_path / "workspace").initialize()
    candidate = ws.load()
    candidate["llm"].update({"backend": "api", "model": "provider-model", "protocol": "responses",
                             "base_url": "https://api.example/v1",
                             "credential": {"kind": "env", "name": "JP_LLM_API_KEY"}})
    seen = {}
    _mock_sdk(monkeypatch, "responses", _response("responses", json.dumps(OUTPUT)), seen)
    configured = settings.configure(ws, candidate, tester=lambda runtime: diagnostics.check(runtime, True))
    assert configured["tested"] is True and seen["calls"] == 1
    before = ws.config_path.read_bytes()

    failed_seen = {}
    _mock_sdk(monkeypatch, "responses", _response("responses", json.dumps({"ok": False})), failed_seen)
    with pytest.raises(ValueError, match="配置测试"):
        settings.configure(ws, candidate, tester=lambda runtime: diagnostics.check(runtime, True))
    assert ws.config_path.read_bytes() == before
    assert failed_seen["calls"] == 1


def test_codex_diagnostics_requires_explicit_model(monkeypatch):
    from jp import diagnostics

    monkeypatch.setattr(diagnostics, "_codex_auth", lambda: (True, "Codex 登录状态检查完成"))
    result = diagnostics.check({"llm": {"backend": "codex", "model": ""}}, test_model=False)
    assert result["status"] == "needs_model" and result["tested"] is False


def test_missing_usage_metrics_remain_unknown(monkeypatch, tmp_path):
    monkeypatch.setenv("JP_LLM_API_KEY", "secret-token-that-must-not-leak")
    api = backends.load_api_config(_config())
    p = packets.create(tmp_path / "tasks", "job", "profile", {"fictional": True}, "prompt", _schema_file(tmp_path))
    response = _response("responses", json.dumps(OUTPUT), usage=types.SimpleNamespace())

    class Client:
        def __init__(self, **kwargs):
            self.responses = types.SimpleNamespace(create=lambda **kw: response)

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=Client))
    result = backends.run_api([p], api)
    assert result.ok == 1 and result.usage_reported == 1
    usage = json.loads((p.dir / "usage.json").read_text(encoding="utf-8"))
    assert usage["input_tokens"] is None
    assert usage["cached_input_tokens"] is None
    assert usage["output_tokens"] is None
