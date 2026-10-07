import copy
import json

import pytest
import yaml

from jp import settings


def test_workspace_initialize_is_idempotent_and_runtime_paths_are_absolute(tmp_path):
    ws = settings.Workspace(tmp_path / "workspace")
    ws.initialize()
    (ws.root / "profile" / "watchlist.yaml").write_text("watchlist: [my-data]\n", encoding="utf-8")
    ws.initialize()
    assert ws.load()["llm"]["backend"] == "manual"
    assert yaml.safe_load((ws.root / "profile" / "watchlist.yaml").read_text(encoding="utf-8"))["watchlist"] == ["my-data"]
    assert json.loads((ws.root / "data" / "facts_index.json").read_text(encoding="utf-8")) == []
    runtime = ws.runtime_config()
    assert all(__import__("pathlib").Path(p).is_absolute() for p in runtime["paths"].values())
    assert runtime["fetch"]["CN"] == runtime["fetch"]["HK"] == {}


def test_validate_deep_copies_and_rejects_secrets_without_echo(tmp_path):
    original = settings._defaults()
    normalized = settings.validate_config(original)
    normalized["llm"]["credential"]["name"] = "CHANGED"
    assert original["llm"]["credential"]["name"] == "JP_LLM_API_KEY"
    original["llm"]["api_key"] = "top-secret-value"
    with pytest.raises(ValueError) as err:
        settings.validate_config(original)
    assert "top-secret-value" not in str(err.value)


def test_configure_tests_before_backup_and_saves_atomically(tmp_path):
    ws = settings.Workspace(tmp_path / "workspace").initialize()
    original = ws.config_path.read_bytes()
    candidate = copy.deepcopy(ws.load())
    candidate["llm"]["model"] = "example-model"

    def reject(_runtime):
        raise RuntimeError("secret should not be exposed")

    with pytest.raises(ValueError, match="配置测试失败"):
        settings.configure(ws, candidate, reject)
    assert ws.config_path.read_bytes() == original
    assert not ws.config_path.with_suffix(".yaml.bak").exists()

    result = settings.configure(ws, candidate, tester=lambda runtime: assert_runtime(runtime))
    assert result["saved"] is True
    assert result["tested"] is True
    assert ws.load()["llm"]["model"] == "example-model"
    assert ws.config_path.with_suffix(".yaml.bak").read_bytes() == original


def assert_runtime(runtime):
    assert runtime["llm"]["api"]["protocol"] == "responses"
    assert runtime["llm"]["api"]["model"] == "example-model"
    assert runtime["llm"]["api"]["concurrency"] == 2
    assert runtime["llm"]["api"]["timeout_sec"] == 180
    assert runtime["llm"]["api"]["reasoning_effort"] == "low"
    assert runtime["paths"]["db"].endswith("jobs.sqlite3")
    return {"status": "configured", "tested": True}


def test_configure_rejects_unsuccessful_test_without_mutation(tmp_path):
    ws = settings.Workspace(tmp_path / "workspace").initialize()
    before = ws.config_path.read_bytes()
    candidate = ws.load()
    with pytest.raises(ValueError, match="未确认成功"):
        settings.configure(ws, candidate, tester=lambda _: {"status": "connection_failed", "tested": True})
    assert ws.config_path.read_bytes() == before


def test_default_workspace_uses_platformdirs_and_override(monkeypatch, tmp_path):
    monkeypatch.delenv("JP_WORKSPACE", raising=False)
    monkeypatch.setattr(settings, "user_data_dir", lambda *args, **kwargs: str(tmp_path / "platformdirs"))
    assert settings.Workspace().root == (tmp_path / "platformdirs").resolve()
    monkeypatch.setenv("JP_WORKSPACE", str(tmp_path / "override"))
    assert settings.Workspace().root == (tmp_path / "override").resolve()


def test_budget_source_blocks_and_api_url_contract():
    cfg = settings._defaults()
    cfg["budget"]["max_jobs"] = 100
    cfg["sources"]["CN"] = ["boss", "watchlist"]
    cfg["fetch"]["CN"]["boss"] = [
        {"keywords": [" AI ", "ML"], "city": "深圳", "track": "intern"},
        {"keywords": ["产品经理"], "track": "campus", "extra": {"job_type": "internship"}},
    ]
    cfg["fetch"]["CN"]["watchlist"] = {}
    normalized = settings.validate_config(cfg)
    assert normalized["fetch"]["CN"]["boss"][0]["keywords"] == ["AI", "ML"]
    cfg["llm"].update({"backend": "api", "model": "demo", "base_url": "https://api.example.com/v1"})
    settings.validate_config(cfg)
    for url in ("http://api.example.com/v1", "https://user:pass@example.com/v1", "https://api.example.com/v1?key=hidden"):
        cfg["llm"]["base_url"] = url
        with pytest.raises(ValueError):
            settings.validate_config(cfg)


def test_enabled_non_watchlist_source_requires_keywords():
    cfg = settings._defaults()
    cfg["sources"]["HK"] = ["linkedin"]
    with pytest.raises(ValueError, match="查询关键词"):
        settings.validate_config(cfg)


def test_credentials_resolve_env_and_session_without_auth_file_access(monkeypatch):
    monkeypatch.setenv("JP_LLM_API_KEY", "in-memory-secret")
    cfg = settings._defaults()
    assert settings.resolve_credentials(cfg) == "in-memory-secret"
    cfg["llm"]["credential"] = {"kind": "session", "name": "session"}
    assert settings.resolve_credentials(cfg, "session-secret") == "session-secret"


def test_agent_prompt_has_workspace_and_guide_paths(tmp_path):
    ws = settings.Workspace(tmp_path / "workspace")
    prompt = settings.agent_prompt(ws)
    assert str(ws.root) in prompt
    assert "agent-setup.md" in prompt
    assert "AGENTS.md" in prompt
