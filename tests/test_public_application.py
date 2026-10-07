from __future__ import annotations

import json
import threading
import time

import pytest
from filelock import Timeout

from board.app import create_app
from jp import db, facts_index, ingest, settings, steps
from jp.models import RawJob, Status
from jp.service import RESOURCES, Service
from jp.tasks import TaskRunner


@pytest.fixture
def public(tmp_path):
    service = Service(settings.Workspace(tmp_path / "workspace"))
    app = create_app(service.config()["paths"]["db"], RESOURCES / "schema.sql", service=service)
    app.config["TESTING"] = True
    client = app.test_client()
    client.get("/setup")
    with client.session_transaction() as session:
        token = session["csrf"]
    return service, client, {"X-CSRF-Token": token}


def test_setup_defaults_and_csrf(public):
    service, client, headers = public
    data = client.get("/api/settings").get_json()
    assert data["config"]["llm"]["backend"] == "manual"
    assert not data["credential_available"]
    assert "docs" in data["agent_prompt"] and str(service.workspace.root) in data["agent_prompt"]
    assert client.post("/api/demo", json={}).status_code == 403
    assert client.post("/api/demo", json={}, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/settings", headers={"Host": "evil.example"}).status_code == 403
    assert client.post("/api/demo", json={}, headers=headers).status_code == 200
    assert "虚构" in client.get("/").get_data(as_text=True)


def test_configuration_failed_test_keeps_previous_and_no_secret(public, monkeypatch):
    service, client, headers = public
    original = service.workspace.config_path.read_bytes()
    candidate = service.workspace.load()
    candidate["llm"].update(backend="api", model="synthetic-model", base_url="https://example.com/v1",
                            credential={"kind": "session", "name": "current"})
    monkeypatch.setattr("jp.diagnostics.check", lambda *_: {"status": "connection_failed", "tested": True, "message": "连接失败"})
    key = "synthetic-private-credential"
    response = client.post("/api/settings", json={"config": candidate, "session_key": key, "test_model": True}, headers=headers)
    assert response.status_code == 400 and key not in response.get_data(as_text=True)
    assert service.workspace.config_path.read_bytes() == original
    assert service.session_key is None
    monkeypatch.setattr("jp.diagnostics.check", lambda *_: {"status": "configured", "tested": True, "message": "成功", "next_step": "继续"})
    response = client.post("/api/settings", json={"config": candidate, "session_key": key, "test_model": True}, headers=headers)
    assert response.status_code == 200
    assert key not in service.workspace.config_path.read_text(encoding="utf-8")
    assert key not in client.get("/api/settings").get_data(as_text=True)
    assert service.session_key == key


def test_model_test_uses_chosen_credential_source(public, monkeypatch):
    service, client, headers = public
    service.session_key = "old-session-must-not-be-used"
    cfg = service.workspace.load()
    cfg["llm"].update(backend="api", model="synthetic", base_url="https://example.com/v1",
                      credential={"kind": "env", "name": "MISSING_TEST_KEY"})
    seen = []

    def diagnostic(runtime, test):
        seen.append(runtime)
        return {"status": "missing_key", "tested": False, "message": "缺少凭据"}

    monkeypatch.setattr("jp.diagnostics.check", diagnostic)
    result = client.post("/api/settings", json={"config": cfg, "test_model": True}, headers=headers)
    assert result.status_code == 400
    assert seen[0]["llm"]["credential"]["kind"] == "env"
    assert "_session_key" not in seen[0]["llm"]["api"]


def test_thread_start_failure_does_not_leave_running_task(public, monkeypatch):
    service, client, headers = public
    runner = client.application.extensions["task_runner"]

    def failed(_):
        raise RuntimeError("synthetic thread startup failure")

    monkeypatch.setattr(threading.Thread, "start", failed)
    with pytest.raises(RuntimeError):
        runner.start("analyze")
    assert runner.list()[0]["status"] == "failed"
    with service.lock:
        pass


def test_resume_can_find_older_task_beyond_recent_page(public, monkeypatch):
    service, client, _ = public
    runner = client.application.extensions["task_runner"]
    with service.connect() as conn:
        for index in range(35):
            conn.execute("INSERT INTO workspace_tasks(id,kind,options,status,created_at) VALUES(?,?,?,'stopped',?)",
                         (str(index), "analyze", "{}", db.now_iso()))
    captured = []
    monkeypatch.setattr(runner, "start", lambda kind, options: captured.append((kind, options)) or "new")
    assert runner.resume("0") == "new"
    assert captured == [("analyze", {})]


def test_manual_resume_confirmation_reaches_runtime_facts(public):
    service, client, headers = public
    text = "示例项目使用 Python 分析数据。电话：13800000000 邮箱：x@example.com"
    response = client.post("/api/profile", json={"text": text}, headers=headers)
    assert response.status_code == 200
    draft = response.get_json()["draft"]
    assert "13800000000" not in draft["text"] and "x@example.com" not in draft["text"]
    from jp import resume
    resume.make_draft(service.workspace, draft["text"], service.config(), True)
    out = {"items": [{"id": "resume.python", "type": "project", "title": "虚构项目", "text": "使用 Python 分析数据",
                       "source_quote": "使用 Python 分析数据", "confirmed": False, "inferred": False}]}
    output = service.workspace.root / "tasks" / "_resume" / "resume" / "output.json"
    output.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    collected = client.post("/api/profile/collect", json={}, headers=headers).get_json()["draft"]
    item = dict(collected["items"][0], confirmed=True)
    response = client.post("/api/profile/confirm", json={"id": draft["id"], "items": [item]}, headers=headers)
    assert response.status_code == 200
    assert facts_index.load(service.config()["paths"]["facts_index"])[0]["id"] == "resume.python"


def test_pipeline_stop_lock_resume_and_preserves_decisions(public, monkeypatch):
    service, client, headers = public
    entered, release = threading.Event(), threading.Event()

    def operation(kind, options, stopped, stage):
        stage("虚构在途请求", {})
        entered.set()
        release.wait(3)
        return {"in_flight_completed": True}

    monkeypatch.setattr(service, "run", operation)
    runner = client.application.extensions["task_runner"]
    tid = runner.start("analyze")
    assert entered.wait(2)
    with pytest.raises(Timeout):
        runner.start("analyze")
    assert client.post("/api/demo", json={}, headers=headers).status_code == 409
    assert client.post("/api/tasks/" + tid + "/stop", json={}, headers=headers).status_code == 200
    release.set()
    for _ in range(100):
        if runner.list()[0]["status"] != "running":
            break
        time.sleep(.01)
    assert runner.list()[0]["status"] == "stopped"
    assert runner.list()[0]["result"]["in_flight_completed"]
    runner.resume(tid)


def test_service_end_to_end_reuses_packets_and_human_choice(public, monkeypatch):
    service, client, headers = public
    from jp import backends
    from tests.test_steps import ANALYZE_OUT, MATCH_OUT
    from tests.test_profile_steps import PROFILE_OUT
    entries = facts_index.build(RESOURCES.parents[1] / "tests" / "fixtures" / "facts")
    facts_index.write(entries, service.config()["paths"]["facts_index"])
    profile = {"facts_hash": steps.facts_hash(entries), **PROFILE_OUT}
    from pathlib import Path
    Path(service.config()["paths"]["candidate_profile"]).write_text(json.dumps(profile), encoding="utf-8")
    cfg = service.workspace.load()
    cfg["llm"].update(backend="api", model="test-model", base_url="https://example.com/v1")
    settings.configure(service.workspace, cfg)
    monkeypatch.setenv("JP_LLM_API_KEY", "synthetic-private-credential")
    calls = []

    def generated(prompt, schema, api, schema_name):
        calls.append(schema_name)
        return MATCH_OUT if schema_name.startswith("match_") else ANALYZE_OUT

    monkeypatch.setattr(backends, "call_api", generated)
    conn = service.connect()
    result = ingest.ingest_jobs(conn, [RawJob(platform_id="test", title="Python 实习", company="虚构公司",
                                            url="https://example.com/test", jd_text="熟悉 Python，了解 RAG", location="深圳")], "referral", "CN")
    conn.close()
    with service.lock:
        service.run("analyze", {"max_jobs": 1})
    jid = result.job_ids[0]
    conn = service.connect()
    assert db.get_job(conn, jid)["status"] == Status.PENDING_REVIEW
    conn.close()
    response = client.post("/decide/" + jid, data={"decision": "apply"}, headers=headers)
    assert response.status_code == 302
    with service.lock:
        service.run("analyze", {"max_jobs": 1})
    conn = service.connect()
    assert db.get_job(conn, jid)["status"] == Status.APPROVED
    conn.close()
    assert len(calls) == 2
