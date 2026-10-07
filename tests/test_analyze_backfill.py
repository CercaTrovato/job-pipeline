from __future__ import annotations
import json
import pathlib
import pytest
from jp import steps, ingest, packets
from jp import db as jpdb
from jp import facts_index as fi
from jp.models import RawJob, Status
from jp.rules import hard
from tests.test_steps import ANALYZE_OUT

ROOT = pathlib.Path(__file__).resolve().parents[1]
FX = ROOT / "tests" / "fixtures" / "facts"

def _ctx(conn, tmp_path):
    return steps.Ctx(conn=conn, tasks_dir=tmp_path / "tasks", specs_dir=ROOT / "llm" / "specs",
                     schemas_dir=ROOT / "llm" / "schemas", facts_entries=fi.build(FX),
                     constraints=hard.load_constraints(ROOT / "tests" / "fixtures" / "constraints.yaml"))

def test_analyze_uses_current_schema_and_backfills(conn, tmp_path):
    ctx = _ctx(conn, tmp_path)
    r = ingest.ingest_jobs(conn, [RawJob(platform_id="x1", title="(待抽取)", company="(待抽取)", url="u", jd_text="美团招聘 AI数据开发工程师，熟悉 Python", location="")], "referral", "CN")
    jid = r.job_ids[0]
    pk = steps.prepare_analyze(ctx)[0]
    assert json.loads((pk.dir / "schema.json").read_text(encoding="utf-8"))["$id"] == "analyze/v3"
    out = dict(ANALYZE_OUT); out["company"] = "美团"; out["title"] = "AI数据开发工程师"
    (pk.dir / "output.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    assert steps.collect_analyze(ctx).done == 1
    row = jpdb.get_job(conn, jid)
    assert (row["company"], row["title"], row["location"]) == ("美团", "AI数据开发工程师", "深圳")

def test_backfill_does_not_overwrite_known_fields(conn, tmp_path):
    ctx = _ctx(conn, tmp_path)
    r = ingest.ingest_jobs(conn, [RawJob(platform_id="x2", title="已知岗位", company="已知公司", url="u", jd_text="jd", location="香港")], "boss", "CN")
    jid = r.job_ids[0]; jpdb.set_status(conn, jid, Status.QUEUED)
    pk = steps.prepare_analyze(ctx)[0]
    out = dict(ANALYZE_OUT); out["company"] = "别的公司"; out["title"] = "别的岗位"
    (pk.dir / "output.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    steps.collect_analyze(ctx)
    row = jpdb.get_job(conn, jid)
    assert (row["company"], row["title"], row["location"]) == ("已知公司", "已知岗位", "香港")

def test_update_job_fields_whitelist(conn):
    r = ingest.ingest_jobs(conn, [RawJob(platform_id="x3", title="t", company="c", url="u", jd_text="jd")], "boss", "CN")
    jpdb.update_job_fields(conn, r.job_ids[0], company="C2", title="T2")
    row = jpdb.get_job(conn, r.job_ids[0])
    assert (row["company"], row["title"]) == ("C2", "T2")
    with pytest.raises(ValueError):
        jpdb.update_job_fields(conn, r.job_ids[0], status="approved")
