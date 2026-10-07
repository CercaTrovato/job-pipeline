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
PROFILE_OUT = json.loads((ROOT / "tests" / "fixtures" / "jd" / "profile_good.json").read_text(encoding="utf-8"))

def _ctx(conn, tmp_path):
    return steps.Ctx(conn=conn, tasks_dir=tmp_path / "tasks", specs_dir=ROOT / "llm" / "specs",
                     schemas_dir=ROOT / "llm" / "schemas", facts_entries=fi.build(FX),
                     constraints=hard.load_constraints(ROOT / "tests" / "fixtures" / "constraints.yaml"),
                     profile_path=tmp_path / "candidate_profile.json")

def test_profile_prepare_and_collect(conn, tmp_path):
    ctx = _ctx(conn, tmp_path)
    pk = steps.prepare_profile(ctx)
    inp = json.loads((pk.dir / "input.json").read_text(encoding="utf-8"))
    assert pk.step == "profile" and len(inp["facts"]) == len(ctx.facts_entries) and inp["facts_hash"]
    assert steps.prepare_profile(ctx).input_hash == pk.input_hash
    (pk.dir / "output.json").write_text(json.dumps(PROFILE_OUT, ensure_ascii=False), encoding="utf-8")
    assert steps.collect_profile(ctx) is True
    prof = steps.load_profile(ctx)
    assert prof["facts_hash"] == inp["facts_hash"] and len(prof["dimensions"]) == 4

def test_profile_rejects_unknown_fact_id(conn, tmp_path):
    ctx = _ctx(conn, tmp_path)
    pk = steps.prepare_profile(ctx)
    bad = json.loads(json.dumps(PROFILE_OUT)); bad["dimensions"][0]["fact_ids"] = ["ghost"]
    (pk.dir / "output.json").write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    assert steps.collect_profile(ctx) is False
    assert packets.status(pk) == "error" and not ctx.profile_path.exists()

def test_prepare_match_uses_profile_and_candidates(conn, tmp_path):
    ctx = _ctx(conn, tmp_path)
    with pytest.raises(RuntimeError):
        steps.load_profile(ctx)
    ctx.profile_path.write_text(json.dumps({"facts_hash": "h", **PROFILE_OUT}, ensure_ascii=False), encoding="utf-8")
    r = ingest.ingest_jobs(conn, [RawJob(platform_id="p1", title="T", company="C", url="u", jd_text="jd", location="深圳")], "boss", "CN")
    jid = r.job_ids[0]; jpdb.set_status(conn, jid, Status.QUEUED)
    pk = steps.prepare_analyze(ctx)[0]
    out = dict(ANALYZE_OUT); out["company"] = None; out["title"] = None
    (pk.dir / "output.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    steps.collect_analyze(ctx)
    mp = steps.prepare_match(ctx)[0]
    inp = json.loads((mp.dir / "input.json").read_text(encoding="utf-8"))
    assert "facts_index" not in inp
    assert inp["profile"]["headline"].startswith("数据科学") and len(inp["profile"]["dimensions"]) == 4
    assert set(inp["candidates"]) == {"R1", "R2"} and "skill.programming" in inp["candidates"]["R1"]
    ids = {f["id"] for f in inp["facts_brief"]}
    assert ids == {i for v in inp["candidates"].values() for i in v}
    assert inp["facts_hash"] == "h"


def test_load_profile_warns_when_stale(conn, tmp_path, capsys):
    ctx = _ctx(conn, tmp_path)
    ctx.profile_path.write_text(json.dumps({"facts_hash": "stale", **PROFILE_OUT}, ensure_ascii=False), encoding="utf-8")
    steps.load_profile(ctx)
    assert "hash 不一致" in capsys.readouterr().out
    ctx.profile_path.write_text(json.dumps({"facts_hash": steps.facts_hash(ctx.facts_entries), **PROFILE_OUT}, ensure_ascii=False), encoding="utf-8")
    steps.load_profile(ctx)
    assert capsys.readouterr().out == ""
