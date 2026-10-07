from __future__ import annotations
import hashlib
import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from jp import candidates
from jp import db as jpdb
from jp import dedup
from jp import packets, prompts, scoring
from jp.models import Status
from jp.rules import hard


@dataclass
class Ctx:
    conn: Any
    tasks_dir: pathlib.Path
    specs_dir: pathlib.Path
    schemas_dir: pathlib.Path
    facts_entries: List[Dict[str, Any]]
    constraints: Dict[str, Any]
    profile_path: pathlib.Path = pathlib.Path("profile/candidate_profile.json")


@dataclass
class CollectResult:
    done: int = 0
    errors: List[str] = field(default_factory=list)


SCHEMA_VERSION = {"analyze": "v3", "match": "v1", "profile": "v1"}
PLACEHOLDER = "(待抽取)"
PROFILE_JOB = "_profile"


def _schema(ctx: Ctx, step: str) -> pathlib.Path:
    return pathlib.Path(ctx.schemas_dir) / ("%s.%s.json" % (step, SCHEMA_VERSION[step]))


def _create(ctx: Ctx, job_id: str, step: str, input_obj: Dict[str, Any]) -> packets.Packet:
    return packets.create(ctx.tasks_dir, job_id, step, input_obj,
                          prompts.render(step, input_obj, ctx.specs_dir), _schema(ctx, step))


def facts_hash(entries: List[Dict[str, Any]]) -> str:
    canon = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


# ---------- analyze ----------

def prepare_analyze(ctx: Ctx, job_ids: Optional[Set[str]] = None) -> List[packets.Packet]:
    out = []
    for row in jpdb.jobs_by_status(ctx.conn, Status.QUEUED):
        if job_ids is not None and row["job_id"] not in job_ids:
            continue
        inp = {"job_id": row["job_id"], "region": row["region"], "jd_lang": row["jd_lang"],
               "company": row["company"], "title": row["title"], "jd_text": row["jd_text"]}
        out.append(_create(ctx, row["job_id"], "analyze", inp))
    return out


def collect_analyze(ctx: Ctx, job_ids: Optional[Set[str]] = None) -> CollectResult:
    res = CollectResult()
    for row in jpdb.jobs_by_status(ctx.conn, Status.QUEUED):
        if job_ids is not None and row["job_id"] not in job_ids:
            continue
        p = packets.load(ctx.tasks_dir, row["job_id"], "analyze")
        if p is None or packets.status(p) != "done":
            continue
        try:
            out = packets.validate(p)
        except packets.PacketError as e:
            res.errors.append("%s analyze: %s" % (row["job_id"], e))
            continue
        ctx.conn.execute(
            "INSERT OR REPLACE INTO analyses(job_id, input_hash, payload, llm_backend, created_at) VALUES(?,?,?,?,?)",
            (row["job_id"], p.input_hash, jpdb.json_dumps(out), "packet", jpdb.now_iso()),
        )
        fields = {}
        if row["company"] == PLACEHOLDER and out.get("company"):
            fields["company"] = out["company"]
        if row["title"] == PLACEHOLDER and out.get("title"):
            fields["title"] = out["title"]
        locs = (out.get("hard_conditions") or {}).get("location") or []
        if not row["location"] and locs:
            fields["location"] = locs[0]
        if fields:
            jpdb.update_job_fields(ctx.conn, row["job_id"], **fields)
        jpdb.set_status(ctx.conn, row["job_id"], Status.ANALYZED)
        res.done += 1
    ctx.conn.commit()
    return res


# ---------- profile ----------

def prepare_profile(ctx: Ctx) -> packets.Packet:
    inp = {"facts": candidates.brief(ctx.facts_entries, [e["id"] for e in ctx.facts_entries]),
           "facts_hash": facts_hash(ctx.facts_entries)}
    return _create(ctx, PROFILE_JOB, "profile", inp)


def collect_profile(ctx: Ctx) -> bool:
    p = packets.load(ctx.tasks_dir, PROFILE_JOB, "profile")
    if p is None or packets.status(p) != "done":
        return False
    known = {e["id"] for e in ctx.facts_entries}
    try:
        out = packets.validate(p)
        unknown = [f for d in out["dimensions"] for f in d["fact_ids"] if f not in known]
        if unknown:
            raise packets.PacketError("画像引用了不存在的 fact_id: %s" % unknown)
    except packets.PacketError as e:
        (p.dir / "error.txt").write_text(str(e), encoding="utf-8")
        if (p.dir / "output.json").exists():
            (p.dir / "output.json").unlink()
        return False
    inp = json.loads((p.dir / "input.json").read_text(encoding="utf-8"))
    ctx.profile_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.profile_path.write_text(json.dumps({"facts_hash": inp["facts_hash"], **out}, ensure_ascii=False, indent=1), encoding="utf-8")
    return True


def load_profile(ctx: Ctx) -> Dict[str, Any]:
    if not ctx.profile_path.exists():
        raise RuntimeError("候选人画像不存在，请先运行 pipeline.py profile-build（并填包 / resume）")
    prof = json.loads(ctx.profile_path.read_text(encoding="utf-8"))
    if prof.get("facts_hash") != facts_hash(ctx.facts_entries):
        print("警告：候选人画像基于旧的 facts（hash 不一致），建议重跑 pipeline.py profile-build 并 resume")
    return prof


# ---------- match ----------

def prepare_match(ctx: Ctx, job_ids: Optional[Set[str]] = None) -> List[packets.Packet]:
    prof = load_profile(ctx)
    out = []
    for row in jpdb.jobs_by_status(ctx.conn, Status.ANALYZED):
        if job_ids is not None and row["job_id"] not in job_ids:
            continue
        a = ctx.conn.execute("SELECT payload FROM analyses WHERE job_id=?", (row["job_id"],)).fetchone()
        analysis = json.loads(a["payload"])
        hr = hard.check_job(row, ctx.constraints, analysis["hard_conditions"], analysis.get("flags"))   # 模型抽取 + 抓取器字段 + 轨道
        _persist_track_flags(ctx, row, hr)
        cands = candidates.candidate_facts(analysis["atomic_requirements"], ctx.facts_entries, k=5)
        ids = {i for v in cands.values() for i in v}
        inp = {"job_id": row["job_id"], "region": row["region"],
               "atomic_requirements": analysis["atomic_requirements"], "keywords": analysis["keywords"],
               "hard_pass": hr.passed, "hard_fail_reasons": hr.reasons, "soft_flags": hr.flags,
               "profile": {"headline": prof["headline"], "dimensions": prof["dimensions"]},
               "candidates": cands, "facts_brief": candidates.brief(ctx.facts_entries, ids),
               "facts_hash": prof["facts_hash"]}
        out.append(_create(ctx, row["job_id"], "match", inp))
    return out


def _check_match(out: Dict[str, Any], inp: Dict[str, Any], known_ids: set) -> None:
    req_ids = {r["id"] for r in inp["atomic_requirements"]}
    item_ids = [it["req_id"] for it in out["items"]]
    got = set(item_ids)
    if len(item_ids) != len(got):
        repeated = sorted({req_id for req_id in item_ids if item_ids.count(req_id) > 1})
        raise packets.PacketError("items req_id 重复: %s" % repeated)
    if got != req_ids:
        raise packets.PacketError("items 未覆盖全部需求，缺: %s 多: %s" % (sorted(req_ids - got), sorted(got - req_ids)))
    cond = {r["id"] for r in inp["atomic_requirements"] if r.get("kind") == "condition"}
    for it in out["items"]:
        if it["req_id"] in cond:
            continue   # 到岗条件类由规则覆盖，fact_ids 会被清空
        unknown = [f for f in it["fact_ids"] if f not in known_ids]
        if unknown:
            raise packets.PacketError("引用了不存在的 fact_id: %s" % unknown)


_DAYS_REQ_RE = re.compile(r"每周|一周|天/周|天\s*/\s*周|到岗|到崗|days?\s*(a|per)\s*week|onsite|on-site", re.I)


def _persist_track_flags(ctx: Ctx, row, hr: hard.HardResult) -> None:
    """把轨道与软标注写回 raw_json（看板展示用）；prescore 也写，这里覆盖成含模型信息的版本。"""
    raw = json.loads(row["raw_json"] or "{}")
    if raw.get("track") == hr.track and raw.get("soft_flags") == hr.flags:
        return
    raw["track"], raw["soft_flags"] = hr.track, hr.flags
    ctx.conn.execute("UPDATE jobs SET raw_json=? WHERE job_id=?", (jpdb.json_dumps(raw), row["job_id"]))
    ctx.conn.commit()      # 立即提交：随后 _dispatch 会用另一条连接写 runs 表，挂着写事务会让它 database is locked


def _apply_condition_rule(inp: Dict[str, Any], out: Dict[str, Any]) -> None:
    """kind=condition 的需求由硬条件规则判定，覆盖模型给出的证据判定，避免"到岗 4 天"这类条件被当成无证据缺口。
    硬条件通过但有"每周 N 天（可谈）"软标注时，到岗类条件项记为 partial（可谈），不当硬伤也不当满足。"""
    cond = {r["id"]: r for r in inp["atomic_requirements"] if r.get("kind") == "condition"}
    if not cond:
        return
    day_flags = [f for f in (inp.get("soft_flags") or []) if "天" in f]
    for it in out["items"]:
        if it["req_id"] in cond:
            if not inp["hard_pass"]:
                it.update(evidence_grade="E", verdict="gap", gap_type="hard", fact_ids=[],
                          note="硬条件不合: " + "；".join(inp.get("hard_fail_reasons") or []))
            elif day_flags and _DAYS_REQ_RE.search(cond[it["req_id"]].get("requirement", "") + cond[it["req_id"]].get("quote", "")):
                it.update(evidence_grade="C", verdict="partial", gap_type=None, fact_ids=[], note="；".join(day_flags) + "，可谈")
            else:
                it.update(evidence_grade="A", verdict="strong", gap_type=None, fact_ids=[], note="规则判定通过")


def collect_match(ctx: Ctx, job_ids: Optional[Set[str]] = None) -> CollectResult:
    res = CollectResult()
    known = {e["id"] for e in ctx.facts_entries}
    for row in jpdb.jobs_by_status(ctx.conn, Status.ANALYZED):
        if job_ids is not None and row["job_id"] not in job_ids:
            continue
        p = packets.load(ctx.tasks_dir, row["job_id"], "match")
        if p is None or packets.status(p) != "done":
            continue
        inp = json.loads((p.dir / "input.json").read_text(encoding="utf-8"))
        try:
            out = packets.validate(p)
            _check_match(out, inp, known)
        except packets.PacketError as e:
            (p.dir / "error.txt").write_text(str(e), encoding="utf-8")
            if (p.dir / "output.json").exists():
                (p.dir / "output.json").unlink()
            res.errors.append("%s match: %s" % (row["job_id"], e))
            continue
        _apply_condition_rule(inp, out)
        score = scoring.soft_score(inp["atomic_requirements"], out["items"], inp["hard_pass"])
        fix = scoring.fixable(out["items"])
        ctx.conn.execute(
            "INSERT OR REPLACE INTO matches(job_id, input_hash, hard_pass, hard_fail_reasons, payload, soft_score, fixable, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (row["job_id"], p.input_hash, int(inp["hard_pass"]), jpdb.json_dumps(inp["hard_fail_reasons"]),
             jpdb.json_dumps(out), score, int(fix), jpdb.now_iso()),
        )
        if not inp["hard_pass"] and row["source"] != "referral":
            jpdb.set_status(ctx.conn, row["job_id"], Status.REJECTED_HARD)
        else:
            jpdb.set_status(ctx.conn, row["job_id"], Status.PENDING_REVIEW)
        res.done += 1
    ctx.conn.commit()
    return res


@dataclass
class RecheckResult:
    rejected: int = 0      # 改为 rejected_hard
    annotated: int = 0     # 内推：只在 matches 上标注硬条件不合、封顶分数，状态不动
    kept: int = 0
    restored: int = 0      # rejected_hard 按现行规则已合格 → 回到 fetched / analyzed / pending_review


def _rescore_match(ctx: Ctx, job_id: str, analysis: Dict[str, Any], out: Dict[str, Any], hard_pass: bool, reasons: List[str],
                   soft_flags: Optional[List[str]] = None) -> None:
    """按硬条件结果重算 match：到岗条件项、封顶分、fixable，与 collect_match 同一套逻辑。"""
    inp = {"atomic_requirements": analysis["atomic_requirements"], "hard_pass": hard_pass, "hard_fail_reasons": reasons,
           "soft_flags": soft_flags or []}
    _apply_condition_rule(inp, out)
    score = scoring.soft_score(analysis["atomic_requirements"], out["items"], hard_pass)
    ctx.conn.execute(
        "UPDATE matches SET hard_pass=?, hard_fail_reasons=?, payload=?, soft_score=?, fixable=? WHERE job_id=?",
        (int(hard_pass), jpdb.json_dumps(reasons), jpdb.json_dumps(out), score, int(scoring.fixable(out["items"])), job_id))


def recheck_hard(ctx: Ctx) -> RecheckResult:
    """用当前规则（含抓取器字段与轨道）重判岗位硬条件，双向生效：

    - queued / analyzed / pending_review 里不合的 → rejected_hard（内推只标注）；已有 match 的重算封顶分与到岗条件项。
    - rejected_hard 里按现行规则已合格的（规则放宽后）→ 恢复：没分析过的回 fetched 重新粗筛；分析过没 match 的回 analyzed；
      有 match 的恢复分数回 pending_review。仍不合的不动。
    规则或抓取器字段口径更新后跑一次即可，幂等。
    """
    res = RecheckResult()
    for status in (Status.QUEUED, Status.ANALYZED, Status.PENDING_REVIEW, Status.REJECTED_HARD):
        for row in jpdb.jobs_by_status(ctx.conn, status):
            a = ctx.conn.execute("SELECT payload FROM analyses WHERE job_id=?", (row["job_id"],)).fetchone()
            analysis = json.loads(a["payload"]) if a else None
            hr = hard.check_job(row, ctx.constraints, analysis["hard_conditions"] if analysis else None,
                                analysis.get("flags") if analysis else None)
            m = ctx.conn.execute("SELECT payload FROM matches WHERE job_id=?", (row["job_id"],)).fetchone()
            raw = json.loads(row["raw_json"] or "{}")
            raw["track"], raw["soft_flags"] = hr.track, hr.flags + dedup.cluster_flags(raw)
            if status == Status.REJECTED_HARD:
                if not hr.passed:
                    continue
                raw.pop("prescreen_reasons", None)
                ctx.conn.execute("UPDATE jobs SET raw_json=? WHERE job_id=?", (jpdb.json_dumps(raw), row["job_id"]))
                if m and analysis:
                    _rescore_match(ctx, row["job_id"], analysis, json.loads(m["payload"]), True, [], hr.flags)
                    jpdb.set_status(ctx.conn, row["job_id"], Status.PENDING_REVIEW)
                elif analysis:
                    jpdb.set_status(ctx.conn, row["job_id"], Status.ANALYZED)
                else:
                    jpdb.set_status(ctx.conn, row["job_id"], Status.FETCHED)
                res.restored += 1
                continue
            if hr.passed:
                ctx.conn.execute("UPDATE jobs SET raw_json=? WHERE job_id=?", (jpdb.json_dumps(raw), row["job_id"]))
                res.kept += 1
                continue
            if m and analysis:
                _rescore_match(ctx, row["job_id"], analysis, json.loads(m["payload"]), False, hr.reasons)
            raw["prescreen_reasons"] = hr.reasons
            ctx.conn.execute("UPDATE jobs SET raw_json=? WHERE job_id=?", (jpdb.json_dumps(raw), row["job_id"]))
            if row["source"] == "referral":
                res.annotated += 1
            else:
                jpdb.set_status(ctx.conn, row["job_id"], Status.REJECTED_HARD)
                res.rejected += 1
    ctx.conn.commit()
    return res


def pending_summary(tasks_dir) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for p in packets.list_pending(tasks_dir):
        out[p.step] = out.get(p.step, 0) + 1
    return out
