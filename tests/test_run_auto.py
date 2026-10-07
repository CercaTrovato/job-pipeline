from __future__ import annotations
import json
import pathlib
import shutil
from types import SimpleNamespace
import yaml
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys; sys.path.insert(0, str(ROOT))
import pipeline  # noqa: E402
from jp import backends, ingest, db as jpdb  # noqa: E402
from jp.models import RawJob  # noqa: E402
from tests.test_steps import ANALYZE_OUT, MATCH_OUT  # noqa: E402

@pytest.fixture
def cfg_path(tmp_path):
    cfg = yaml.safe_load((ROOT / "tests" / "fixtures" / "config.yaml").read_text(encoding="utf-8"))
    cfg["paths"]["db"] = str(tmp_path / "e2e.sqlite")
    cfg["paths"]["tasks_dir"] = str(tmp_path / "tasks")
    cfg["paths"]["facts_index"] = str(tmp_path / "facts_index.json")
    cfg["paths"]["resume_repo"] = str(tmp_path / "resume")
    cfg["paths"]["candidate_profile"] = str(tmp_path / "candidate_profile.json")
    cfg["llm"]["backend"] = "api"
    cfg["llm"]["api"].update({"protocol": "responses", "base_url": "https://fake/v1",
                               "model": "m", "concurrency": 2})
    shutil.copytree(ROOT / "tests" / "fixtures" / "facts", tmp_path / "resume" / "facts")
    prof = json.loads((ROOT / "tests" / "fixtures" / "jd" / "profile_good.json").read_text(encoding="utf-8"))
    (tmp_path / "candidate_profile.json").write_text(json.dumps({"facts_hash": "h", **prof}, ensure_ascii=False), encoding="utf-8")
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return p

def test_run_goes_to_pending_review_with_api(cfg_path, monkeypatch, capsys):
    monkeypatch.setenv("JP_LLM_API_KEY", "sk-test")
    def fake_call_api(prompt, schema, api, schema_name):
        if "match" in schema_name:
            return MATCH_OUT
        out = dict(ANALYZE_OUT, company="美团", title="AI数据开发工程师")
        out["atomic_requirements"] = [dict(req) for req in ANALYZE_OUT["atomic_requirements"]]
        out["atomic_requirements"][1]["quote"] = "开发 RAG" if "基于 GLM 系列模型开发 RAG" in prompt else "了解 RAG"
        return out
    monkeypatch.setattr(backends, "call_api", fake_call_api)
    run = lambda *a: pipeline.main(["--config", str(cfg_path), *a])
    run("init-db"); run("facts-index")
    run("ingest", "--source", "referral", "--region", "CN", "--file", str(ROOT / "tests" / "fixtures" / "jd" / "referral_cn.json"))
    run("run")
    out = capsys.readouterr().out
    assert "api 已执行 1 个包" in out and "match 回收: 1 成功" in out
    run("review")
    out = capsys.readouterr().out
    assert "100.0" in out and "美团" not in out   # referral 自带公司名，不回填
    run("run")
    assert "没有待填任务包" in capsys.readouterr().out


def test_run_goes_to_pending_review_with_codex_subscription(cfg_path, monkeypatch, capsys):
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg["llm"]["backend"] = "codex"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(backends, "call_api", lambda *args: pytest.fail("第三方 API 不应被调用"))
    seen = []

    def fake_exec(cmd, input, timeout, capture_output):
        seen.append(cmd)
        out = MATCH_OUT if "证据等级".encode("utf-8") in input else dict(
            ANALYZE_OUT, company="美团", title="AI数据开发工程师")
        pathlib.Path(cmd[cmd.index("-o") + 1]).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        return type("Result", (), {"returncode": 0, "stderr": b"", "stdout": b""})()

    monkeypatch.setattr(backends.subprocess, "run", fake_exec)
    run = lambda *a: pipeline.main(["--config", str(cfg_path), *a])
    run("init-db"); run("facts-index")
    run("ingest", "--source", "referral", "--region", "CN", "--file", str(ROOT / "tests" / "fixtures" / "jd" / "referral_cn.json"))
    conn = jpdb.connect(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["paths"]["db"])
    ingest.ingest_jobs(conn, [RawJob(platform_id="second", title="数据分析实习", company="另一家公司",
                                    url="https://example.org/second", jd_text="熟悉 Python，了解 RAG", location="深圳")],
                       source="referral", region="CN")
    chosen_id = conn.execute("SELECT job_id FROM jobs WHERE title=?", ("数据分析实习",)).fetchone()["job_id"]
    conn.close()
    monkeypatch.setattr(pipeline, "cmd_dedup", lambda *_: pytest.fail("定向处理不应重跑全库去重"))
    monkeypatch.setattr(pipeline, "cmd_prescore", lambda *_: pytest.fail("定向处理不应从候补池补队列"))
    run("run", "--max-jobs", "1", "--job-id", chosen_id)
    out = capsys.readouterr().out
    assert "codex 已执行 1 个包" in out and "match 回收: 1 成功" in out
    assert len(seen) == 2 and all("--ignore-user-config" in cmd for cmd in seen)
    conn = jpdb.connect(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["paths"]["db"])
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM jobs GROUP BY status")}
    chosen_status = conn.execute("SELECT status FROM jobs WHERE job_id=?", (chosen_id,)).fetchone()["status"]
    conn.close()
    assert counts["pending_review"] == 1 and counts["queued"] == 1
    assert chosen_status == "pending_review"


def test_codex_run_cannot_exceed_three_jobs(monkeypatch):
    monkeypatch.setattr(pipeline, "cmd_dedup", lambda args, cfg: None)
    monkeypatch.setattr(pipeline, "cmd_prescore", lambda args, cfg: None)
    monkeypatch.setattr(pipeline, "_ctx", lambda cfg: None)
    cfg = {"llm": {"backend": "codex", "codex_job_budget": 3}}
    with pytest.raises(SystemExit, match="最多处理 3 条"):
        pipeline.cmd_run(SimpleNamespace(fetch=False, max_jobs=4), cfg)
    cfg["llm"]["backend"] = "api"
    with pytest.raises(SystemExit, match="最多处理 3 条"):
        pipeline.cmd_run(SimpleNamespace(fetch=False, max_jobs=4), cfg)
