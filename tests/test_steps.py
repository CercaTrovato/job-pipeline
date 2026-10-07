from __future__ import annotations
import json
import pathlib
from jp import steps, ingest, packets
from jp import facts_index as fi
from jp.models import RawJob, Status
from jp.rules import hard

ROOT = pathlib.Path(__file__).resolve().parents[1]
FX = ROOT / "tests" / "fixtures" / "facts"

ANALYZE_OUT = {
    "atomic_requirements": [
        {"id": "R1", "quote": "熟悉 Python", "requirement": "Python 编程", "level": "high", "kind": "skill"},
        {"id": "R2", "quote": "了解 RAG", "requirement": "RAG 应用", "level": "mid", "kind": "skill"},
    ],
    "keywords": {"must": ["Python"], "core": ["RAG"], "bonus": []},
    "hard_conditions": {"location": ["深圳"], "days_per_week": 4, "min_months": 3, "employment_type": "internship",
                        "graduated_required": False, "onsite_days": None, "visa": "unknown", "language": []},
    "flags": {"entry_level": True, "remote": False, "full_time": False},
    "summary": "深圳 LLM 应用实习，要求 Python 与 RAG。",
    "company": None, "title": None,
}
MATCH_OUT = {
    "items": [
        {"req_id": "R1", "evidence_grade": "A", "fact_ids": ["skill.programming"], "verdict": "strong", "gap_type": None, "note": "多条证据"},
        {"req_id": "R2", "evidence_grade": "C", "fact_ids": ["bank.rag"], "verdict": "strong", "gap_type": None, "note": "口述证据"},
    ],
    "rationale": "整体值得投：Python 有 A 级证据，RAG 有银行实习的口述证据，无硬门槛问题。",
}
PROFILE_OUT = json.loads((ROOT / "tests" / "fixtures" / "jd" / "profile_good.json").read_text(encoding="utf-8"))

def _ctx(conn, tmp_path):
    return steps.Ctx(conn=conn, tasks_dir=tmp_path / "tasks", specs_dir=ROOT / "llm" / "specs",
                     schemas_dir=ROOT / "llm" / "schemas", facts_entries=fi.build(FX),
                     constraints=hard.load_constraints(ROOT / "tests" / "fixtures" / "constraints.yaml"),
                     profile_path=tmp_path / "candidate_profile.json")

def _seed_profile(ctx):
    ctx.profile_path.write_text(json.dumps({"facts_hash": "h", **PROFILE_OUT}, ensure_ascii=False), encoding="utf-8")

def _seed(conn, source="boss", status_after=None):
    r = ingest.ingest_jobs(conn, [RawJob(platform_id="p1", title="LLM 实习", company="C", url="u", jd_text="熟悉 Python，了解 RAG", location="深圳")],
                           source=source, region="CN")
    jid = r.job_ids[0]
    from jp import db as jpdb
    jpdb.set_status(conn, jid, Status.QUEUED)
    return jid

def _fill(p, obj):
    (p.dir / "output.json").write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

def test_full_chain_to_pending_review(conn, tmp_path):
    ctx = _ctx(conn, tmp_path)
    _seed_profile(ctx)
    jid = _seed(conn)
    pk = steps.prepare_analyze(ctx)
    assert [p.job_id for p in pk] == [jid]
    assert json.loads((pk[0].dir / "input.json").read_text(encoding="utf-8"))["jd_text"] == "熟悉 Python，了解 RAG"
    assert steps.prepare_analyze(ctx)[0].input_hash == pk[0].input_hash   # 幂等
    _fill(pk[0], ANALYZE_OUT)
    r = steps.collect_analyze(ctx)
    assert r.done == 1 and r.errors == []
    assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (jid,)).fetchone()[0] == Status.ANALYZED

    mp = steps.prepare_match(ctx)
    inp = json.loads((mp[0].dir / "input.json").read_text(encoding="utf-8"))
    assert inp["hard_pass"] is True and "facts_index" not in inp
    _fill(mp[0], MATCH_OUT)
    r2 = steps.collect_match(ctx)
    assert r2.done == 1
    row = conn.execute("SELECT status FROM jobs WHERE job_id=?", (jid,)).fetchone()
    m = conn.execute("SELECT soft_score, fixable, hard_pass FROM matches WHERE job_id=?", (jid,)).fetchone()
    assert row["status"] == Status.PENDING_REVIEW and m["soft_score"] == 100.0 and m["fixable"] == 1 and m["hard_pass"] == 1


def test_collect_respects_target_job_ids(conn, tmp_path):
    ctx = _ctx(conn, tmp_path)
    _seed_profile(ctx)
    first = _seed(conn)
    second = ingest.ingest_jobs(conn, [RawJob(platform_id="p2", title="RAG 实习", company="D",
                                              url="u2", jd_text="熟悉 Python，了解 RAG", location="深圳")],
                                source="boss", region="CN").job_ids[0]
    from jp import db as jpdb
    jpdb.set_status(conn, second, Status.QUEUED)
    assert [p.job_id for p in steps.prepare_analyze(ctx, {first})] == [first]
    for packet in steps.prepare_analyze(ctx):
        _fill(packet, ANALYZE_OUT)
    assert steps.collect_analyze(ctx, {first}).done == 1
    assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (second,)).fetchone()[0] == Status.QUEUED
    assert steps.collect_analyze(ctx, {second}).done == 1
    assert [p.job_id for p in steps.prepare_match(ctx, {first})] == [first]
    for packet in steps.prepare_match(ctx):
        _fill(packet, MATCH_OUT)
    assert steps.collect_match(ctx, {first}).done == 1
    assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (second,)).fetchone()[0] == Status.ANALYZED

def test_hard_fail_rejects_unless_referral(conn, tmp_path):
    ctx = _ctx(conn, tmp_path)
    _seed_profile(ctx)
    jid = _seed(conn)
    pk = steps.prepare_analyze(ctx)
    bad = json.loads(json.dumps(ANALYZE_OUT)); bad["hard_conditions"]["min_months"] = 8
    _fill(pk[0], bad); steps.collect_analyze(ctx)
    mp = steps.prepare_match(ctx)
    assert json.loads((mp[0].dir / "input.json").read_text(encoding="utf-8"))["hard_fail_reasons"] == ["要求 ≥8 个月"]
    _fill(mp[0], MATCH_OUT); steps.collect_match(ctx)
    assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (jid,)).fetchone()[0] == Status.REJECTED_HARD
    assert conn.execute("SELECT soft_score FROM matches WHERE job_id=?", (jid,)).fetchone()[0] == 30.0

def test_unknown_fact_id_is_error(conn, tmp_path):
    ctx = _ctx(conn, tmp_path)
    _seed_profile(ctx)
    jid = _seed(conn)
    pk = steps.prepare_analyze(ctx); _fill(pk[0], ANALYZE_OUT); steps.collect_analyze(ctx)
    mp = steps.prepare_match(ctx)
    bad = json.loads(json.dumps(MATCH_OUT)); bad["items"][0]["fact_ids"] = ["ghost.id"]
    _fill(mp[0], bad)
    r = steps.collect_match(ctx)
    assert r.done == 0 and "ghost.id" in r.errors[0]
    assert packets.status(mp[0]) == "error"
    assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (jid,)).fetchone()[0] == Status.ANALYZED

def test_items_must_cover_all_reqs(conn, tmp_path):
    ctx = _ctx(conn, tmp_path)
    _seed_profile(ctx)
    _seed(conn)
    pk = steps.prepare_analyze(ctx); _fill(pk[0], ANALYZE_OUT); steps.collect_analyze(ctx)
    mp = steps.prepare_match(ctx)
    bad = {"items": MATCH_OUT["items"][:1], "rationale": MATCH_OUT["rationale"]}
    _fill(mp[0], bad)
    r = steps.collect_match(ctx)
    assert r.done == 0 and "R2" in r.errors[0]


def test_duplicate_match_req_id_is_rejected(conn, tmp_path):
    ctx = _ctx(conn, tmp_path)
    _seed_profile(ctx)
    jid = _seed(conn)
    pk = steps.prepare_analyze(ctx); _fill(pk[0], ANALYZE_OUT); steps.collect_analyze(ctx)
    mp = steps.prepare_match(ctx)
    bad = json.loads(json.dumps(MATCH_OUT))
    bad["items"].append(dict(bad["items"][0]))
    _fill(mp[0], bad)
    result = steps.collect_match(ctx)
    assert result.done == 0 and "req_id 重复" in result.errors[0]
    assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (jid,)).fetchone()[0] == Status.ANALYZED


def test_condition_requirements_follow_hard_rules(conn, tmp_path):
    ctx = _ctx(conn, tmp_path)
    ctx.profile_path.write_text(json.dumps({"facts_hash": "h", **PROFILE_OUT}, ensure_ascii=False), encoding="utf-8")
    jid = _seed(conn)
    pk = steps.prepare_analyze(ctx)[0]
    assert json.loads((pk.dir / "schema.json").read_text(encoding="utf-8"))["$id"] == "analyze/v3"
    a = json.loads(json.dumps(ANALYZE_OUT))
    a["atomic_requirements"].append({"id": "R3", "quote": "每周到岗 4 天", "requirement": "每周到岗 4 天", "level": "high", "kind": "condition"})
    _fill(pk, a); steps.collect_analyze(ctx)
    mp = steps.prepare_match(ctx)[0]
    m = json.loads(json.dumps(MATCH_OUT))
    m["items"].append({"req_id": "R3", "evidence_grade": "E", "fact_ids": [], "verdict": "gap", "gap_type": "evidence", "note": "索引无到岗信息"})
    _fill(mp, m); assert steps.collect_match(ctx).done == 1
    row = conn.execute("SELECT soft_score, payload FROM matches WHERE job_id=?", (jid,)).fetchone()
    items = {it["req_id"]: it for it in json.loads(row["payload"])["items"]}
    assert items["R3"]["verdict"] == "strong" and items["R3"]["note"] == "规则判定通过"
    assert row["soft_score"] == 100.0


def test_condition_requirements_hard_fail_and_fact_ids_ignored(conn, tmp_path):
    ctx = _ctx(conn, tmp_path)
    _seed_profile(ctx)
    jid = _seed(conn)
    pk = steps.prepare_analyze(ctx)[0]
    a = json.loads(json.dumps(ANALYZE_OUT))
    a["hard_conditions"]["min_months"] = 8
    a["atomic_requirements"].append({"id": "R3", "quote": "实习 8 个月以上", "requirement": "实习 8 个月以上", "level": "high", "kind": "condition"})
    _fill(pk, a); steps.collect_analyze(ctx)
    mp = steps.prepare_match(ctx)[0]
    m = json.loads(json.dumps(MATCH_OUT))
    m["items"].append({"req_id": "R3", "evidence_grade": "C", "fact_ids": ["ghost.id"], "verdict": "partial", "gap_type": "evidence", "note": ""})
    _fill(mp, m); assert steps.collect_match(ctx).done == 1
    row = conn.execute("SELECT soft_score, payload FROM matches WHERE job_id=?", (jid,)).fetchone()
    it = {x["req_id"]: x for x in json.loads(row["payload"])["items"]}["R3"]
    assert (it["verdict"], it["gap_type"], it["fact_ids"]) == ("gap", "hard", []) and "要求 ≥8 个月" in it["note"]
    assert row["soft_score"] == 30.0


def test_days_condition_becomes_negotiable_partial_not_hard_fail(conn, tmp_path):
    """每周 5 天：不再硬淘汰；到岗条件项标为 partial（可谈），分数不封顶，soft_flags 落到 raw_json 供看板展示。"""
    ctx = _ctx(conn, tmp_path)
    _seed_profile(ctx)
    jid = _seed(conn)
    pk = steps.prepare_analyze(ctx)[0]
    a = json.loads(json.dumps(ANALYZE_OUT))
    a["hard_conditions"]["days_per_week"] = 5
    a["atomic_requirements"].append({"id": "R3", "quote": "每周到岗 5 天", "requirement": "每周到岗 5 天", "level": "high", "kind": "condition"})
    _fill(pk, a); steps.collect_analyze(ctx)
    mp = steps.prepare_match(ctx)[0]
    inp = json.loads((mp.dir / "input.json").read_text(encoding="utf-8"))
    assert inp["hard_pass"] is True and inp["soft_flags"] == ["要求每周 5 天（可谈）"]
    m = json.loads(json.dumps(MATCH_OUT))
    m["items"].append({"req_id": "R3", "evidence_grade": "C", "fact_ids": [], "verdict": "partial", "gap_type": "evidence", "note": ""})
    _fill(mp, m); assert steps.collect_match(ctx).done == 1
    row = conn.execute("SELECT soft_score, payload FROM matches WHERE job_id=?", (jid,)).fetchone()
    it = {x["req_id"]: x for x in json.loads(row["payload"])["items"]}["R3"]
    assert (it["verdict"], it["gap_type"]) == ("partial", None) and "可谈" in it["note"]
    assert row["soft_score"] == 83.3
    assert json.loads(conn.execute("SELECT raw_json FROM jobs WHERE job_id=?", (jid,)).fetchone()[0])["soft_flags"] == ["要求每周 5 天（可谈）"]
    assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (jid,)).fetchone()[0] == Status.PENDING_REVIEW


def _seed_raw(conn, title, location, raw, source="watchlist"):
    r = ingest.ingest_jobs(conn, [RawJob(platform_id="p-" + title, title=title, company="C", url="u", jd_text="熟悉 Python，了解 RAG",
                                          location=location, raw=raw)], source=source, region="CN")
    from jp import db as jpdb
    jpdb.set_status(conn, r.job_ids[0], Status.QUEUED)
    return r.job_ids[0]


def _through_analyze(ctx, analyze_out):
    pk = steps.prepare_analyze(ctx)
    for p in pk:
        _fill(p, analyze_out)
    steps.collect_analyze(ctx)


def test_prepare_match_uses_adapter_location_when_model_has_none(conn, tmp_path):
    ctx = _ctx(conn, tmp_path)
    _seed_profile(ctx)
    jid = _seed_raw(conn, "MaaS 平台开发工程师", "北京，上海", {"recruit_type": "全职"})
    a = json.loads(json.dumps(ANALYZE_OUT)); a["hard_conditions"].update(location=[], employment_type="unknown", days_per_week=None, min_months=None)
    _through_analyze(ctx, a)
    inp = json.loads((steps.prepare_match(ctx)[0].dir / "input.json").read_text(encoding="utf-8"))
    assert inp["hard_pass"] is False
    assert "地点不符: 北京/上海" in inp["hard_fail_reasons"] and "雇佣类型不符: fulltime" in inp["hard_fail_reasons"]
    assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (jid,)).fetchone()[0] == Status.ANALYZED


def test_prepare_match_campus_track_allows_chengdu_fulltime(conn, tmp_path):
    ctx = _ctx(conn, tmp_path)
    _seed_profile(ctx)
    _seed_raw(conn, "算法工程师（2027届校招）", "成都", {"recruit_type": "全职"})
    a = json.loads(json.dumps(ANALYZE_OUT)); a["hard_conditions"].update(location=["成都"], employment_type="fulltime", days_per_week=5, min_months=None)
    _through_analyze(ctx, a)
    inp = json.loads((steps.prepare_match(ctx)[0].dir / "input.json").read_text(encoding="utf-8"))
    assert inp["hard_pass"] is True and inp["hard_fail_reasons"] == []


def test_recheck_hard_moves_pending_review_and_caps_score(conn, tmp_path):
    """库里已进待审但按岗位级字段不合硬条件的记录：recheck 把它们改为硬条件不合、分数封顶；合规的不动；内推只标注。"""
    ctx = _ctx(conn, tmp_path)
    _seed_profile(ctx)
    bad = _seed_raw(conn, "MaaS 平台开发工程师", "北京，上海", {"recruit_type": "全职"})
    good = _seed_raw(conn, "LLM 实习", "深圳", {})
    ref = _seed_raw(conn, "内推岗位", "北京", {}, source="referral")
    a = json.loads(json.dumps(ANALYZE_OUT)); a["hard_conditions"].update(location=[], employment_type="unknown")
    _through_analyze(ctx, a)
    # 模拟旧逻辑：三条都以 hard_pass=True 进了待审
    for p in steps.prepare_match(ctx):
        inp = json.loads((p.dir / "input.json").read_text(encoding="utf-8"))
        inp.update(hard_pass=True, hard_fail_reasons=[])
        (p.dir / "input.json").write_text(json.dumps(inp, ensure_ascii=False), encoding="utf-8")
        _fill(p, MATCH_OUT)
    steps.collect_match(ctx)
    assert {r[0] for r in conn.execute("SELECT status FROM jobs")} == {Status.PENDING_REVIEW}
    res = steps.recheck_hard(ctx)
    assert (res.rejected, res.annotated, res.kept) == (1, 1, 1)
    st = {r["job_id"]: r["status"] for r in conn.execute("SELECT job_id, status FROM jobs")}
    assert st[bad] == Status.REJECTED_HARD and st[good] == Status.PENDING_REVIEW and st[ref] == Status.PENDING_REVIEW
    m = conn.execute("SELECT hard_pass, hard_fail_reasons, soft_score FROM matches WHERE job_id=?", (bad,)).fetchone()
    assert m["hard_pass"] == 0 and "地点不符: 北京/上海" in json.loads(m["hard_fail_reasons"]) and m["soft_score"] == 30.0
    m2 = conn.execute("SELECT hard_pass, soft_score FROM matches WHERE job_id=?", (ref,)).fetchone()
    assert m2["hard_pass"] == 0 and m2["soft_score"] == 30.0
    assert conn.execute("SELECT soft_score FROM matches WHERE job_id=?", (good,)).fetchone()[0] == 100.0
    assert steps.recheck_hard(ctx).rejected == 0   # 幂等


def test_recheck_hard_restores_rejected_when_rules_relaxed(conn, tmp_path):
    """规则放宽后：粗筛阶段被硬伤的记录回到 fetched 重新粗筛；分析后被硬伤的记录恢复分数并回到待审；仍不合的不动。"""
    ctx = _ctx(conn, tmp_path)
    _seed_profile(ctx)
    from jp import db as jpdb
    # ① 粗筛阶段硬伤（无 analyses）：按当前规则 4 个月已可接受
    r1 = ingest.ingest_jobs(conn, [RawJob(platform_id="m4", title="LLM 实习", company="C", url="u", jd_text="熟悉 Python，了解 RAG",
                                          location="深圳", raw={"days_per_week": 4, "min_months": 4})], source="boss", region="CN")
    early = r1.job_ids[0]
    conn.execute("UPDATE jobs SET raw_json=? WHERE job_id=?",
                 (jpdb.json_dumps({"days_per_week": 4, "min_months": 4, "track": "intern", "prescreen_reasons": ["要求 ≥4 个月"]}), early))
    jpdb.set_status(conn, early, Status.REJECTED_HARD)
    # ② 分析 + 匹配后硬伤：模型当时抽到 min_months=5，被旧口径（≤3）封顶淘汰
    late = _seed_raw(conn, "多模态实习生", "深圳", {"days_per_week": 4, "min_months": 5})
    a = json.loads(json.dumps(ANALYZE_OUT)); a["hard_conditions"].update(min_months=5)
    a["atomic_requirements"].append({"id": "R3", "quote": "实习 5 个月", "requirement": "实习 5 个月", "level": "high", "kind": "condition"})
    _through_analyze(ctx, a)
    mp = steps.prepare_match(ctx)[0]
    inp = json.loads((mp.dir / "input.json").read_text(encoding="utf-8"))
    inp.update(hard_pass=False, hard_fail_reasons=["要求 ≥5 个月"])
    (mp.dir / "input.json").write_text(json.dumps(inp, ensure_ascii=False), encoding="utf-8")
    m = json.loads(json.dumps(MATCH_OUT))
    m["items"].append({"req_id": "R3", "evidence_grade": "E", "fact_ids": [], "verdict": "gap", "gap_type": "hard", "note": ""})
    _fill(mp, m); steps.collect_match(ctx)
    assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (late,)).fetchone()[0] == Status.REJECTED_HARD
    # ③ 仍然不合的：每周 5 天
    still = ingest.ingest_jobs(conn, [RawJob(platform_id="d5", title="LLM 实习", company="C", url="u", jd_text="实习 9 个月以上",
                                             location="深圳", raw={"min_months": 9})], source="boss", region="CN").job_ids[0]
    jpdb.set_status(conn, still, Status.REJECTED_HARD)

    res = steps.recheck_hard(ctx)
    assert res.restored == 2 and res.rejected == 0
    st = {r["job_id"]: r["status"] for r in conn.execute("SELECT job_id, status FROM jobs")}
    assert st[early] == Status.FETCHED and st[late] == Status.PENDING_REVIEW and st[still] == Status.REJECTED_HARD
    assert "prescreen_reasons" not in json.loads(conn.execute("SELECT raw_json FROM jobs WHERE job_id=?", (early,)).fetchone()[0])
    mrow = conn.execute("SELECT hard_pass, hard_fail_reasons, soft_score, payload FROM matches WHERE job_id=?", (late,)).fetchone()
    assert mrow["hard_pass"] == 1 and json.loads(mrow["hard_fail_reasons"]) == [] and mrow["soft_score"] == 100.0
    r3 = {x["req_id"]: x for x in json.loads(mrow["payload"])["items"]}["R3"]
    assert (r3["verdict"], r3["gap_type"]) == ("strong", None)
