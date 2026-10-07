from __future__ import annotations
import datetime as dt
import itertools
import json
from collections import defaultdict
from typing import Any, Dict, List

from jp import db as jpdb
from jp.models import Status
from jp.selection_duplicates import (compare_rows, employer_requisition_ids,
                                     find_approved_conflicts, normalized_jd)

_MAP = {"apply": Status.APPROVED, "skip": Status.SKIPPED, "later": Status.LATER}
_STATUS_LABELS = {Status.APPROVED: "已选待投递", Status.SKIPPED: "已跳过",
                  Status.PENDING_REVIEW: "待挑选", Status.QUEUED: "待分析",
                  Status.ANALYZED: "待匹配", Status.FETCHED: "候补（未分析）",
                  Status.PRESCREENED_OUT: "粗筛淘汰", Status.REJECTED_HARD: "硬条件不合",
                  Status.DUPLICATE: "重复帖"}


class DuplicateConfirmationRequired(ValueError):
    def __init__(self, conflicts: List[Dict[str, Any]], allow_override: bool = True):
        super().__init__("发现同招聘品牌的已选相似岗位，请先核对再确认")
        self.conflicts = conflicts
        self.allow_override = allow_override


def apply(conn, job_id: str, decision: str, note: str = "", later_days: int = 7,
          confirm_duplicate: bool = False) -> str:
    if decision not in _MAP:
        raise ValueError("decision 必须是 apply/skip/later")
    job = jpdb.get_job(conn, job_id)
    if job is None or job["status"] != Status.PENDING_REVIEW:
        raise ValueError("只能裁决 pending_review 状态的岗位: %s" % job_id)
    conflicts = find_approved_conflicts(conn, job) if decision == "apply" else []
    if conflicts and not confirm_duplicate:
        raise DuplicateConfirmationRequired(conflicts)
    until = None
    if decision == "later":
        until = (dt.datetime.now() + dt.timedelta(days=later_days)).replace(microsecond=0).isoformat()
    conn.execute("INSERT INTO decisions(job_id, decision, note, decided_at, later_until) VALUES(?,?,?,?,?)",
                 (job_id, decision, note, jpdb.now_iso(), until))
    for conflict in conflicts:
        conn.execute("INSERT INTO duplicate_reviews(job_id, related_job_id, action, note, created_at) "
                     "VALUES(?,?,?,?,?)", (job_id, conflict["related_job_id"], "keep_both",
                                      "选择投递时已确认相似岗位", jpdb.now_iso()))
    jpdb.set_status(conn, job_id, _MAP[decision])
    return _MAP[decision]


def release_later(conn) -> int:
    now = jpdb.now_iso()
    rows = conn.execute(
        "SELECT j.job_id FROM jobs j JOIN decisions d ON d.job_id=j.job_id "
        "WHERE j.status='later' AND d.decision='later' AND d.later_until <= ? "
        "AND d.decided_at = (SELECT MAX(decided_at) FROM decisions WHERE job_id=j.job_id)", (now,)).fetchall()
    for r in rows:
        jpdb.set_status(conn, r["job_id"], Status.PENDING_REVIEW)
    return len(rows)


def requeue(conn, job_id: str, confirm_distinct: bool = False) -> None:
    job = jpdb.get_job(conn, job_id)
    if job is None or job["status"] not in (Status.FETCHED, Status.PRESCREENED_OUT,
                                            Status.REJECTED_HARD, Status.DUPLICATE):
        raise ValueError("只能重新入队 fetched（候补）/ prescreened_out / rejected_hard / duplicate 的岗位")
    exact = [item for item in find_approved_conflicts(conn, job) if item["kind"] == "exact"]
    if exact:
        distinct_official = all(_distinct_official_job(conn, job, item["related_job_id"]) for item in exact)
        if not confirm_distinct or not distinct_official:
            raise DuplicateConfirmationRequired(exact, allow_override=distinct_official)
    raw = json.loads(job["raw_json"] or "{}")
    if job["status"] == Status.DUPLICATE:          # 人工要的就是这条，之后 dedup 不再把它折叠回去
        raw["dup_keep"] = True
        raw["requeued_from_dup_of"] = raw.get("dup_of")
        raw["requeued_dup_reason"] = raw.get("dup_reason")
        raw.pop("dup_of", None)
        raw.pop("dup_reason", None)
    raw["manual_requeued_at"] = jpdb.now_iso()
    if confirm_distinct:
        confirmed = set(raw.get("confirmed_distinct_related_ids") or [])
        confirmed.update(item["related_job_id"] for item in exact)
        raw["confirmed_distinct_related_ids"] = sorted(confirmed)
    conn.execute("UPDATE jobs SET raw_json=? WHERE job_id=?", (jpdb.json_dumps(raw), job_id))
    jpdb.set_status(conn, job_id, Status.QUEUED)


def _distinct_official_job(conn, job, related_id: str) -> bool:
    other = jpdb.get_job(conn, related_id)
    if not other:
        return False
    mine, theirs = employer_requisition_ids(job), employer_requisition_ids(other)
    if set(mine).intersection(theirs):
        return False
    return any(namespace == other_namespace and value != other_value
               for namespace, value in mine for other_namespace, other_value in theirs)


def set_manual_priority(conn, job_id: str, priority: int) -> None:
    """Pin an already manually requeued job without changing its review status."""
    job = jpdb.get_job(conn, job_id)
    if not job or job["status"] != Status.QUEUED or priority < 0:
        raise ValueError("只能调整已入队岗位的非负人工优先级")
    raw = json.loads(job["raw_json"] or "{}")
    if not (raw.get("manual_requeued_at") or raw.get("dup_keep")):
        raise ValueError("岗位并非人工重新入队")
    raw["manual_priority"] = priority
    conn.execute("UPDATE jobs SET raw_json=? WHERE job_id=?", (jpdb.json_dumps(raw), job_id))
    conn.commit()


def approved_duplicate_pairs(conn) -> List[Dict[str, Any]]:
    """Read-only review candidates; no approved decision is changed by detection."""
    rows = conn.execute("SELECT * FROM jobs WHERE status=? ORDER BY job_id", (Status.APPROVED,)).fetchall()
    pairs = []
    for left, right in itertools.combinations(rows, 2):
        conflict = compare_rows(left, right)
        if not conflict:
            continue
        review = conn.execute(
            "SELECT action, note, created_at FROM duplicate_reviews "
            "WHERE (job_id=? AND related_job_id=?) OR (job_id=? AND related_job_id=?) "
            "ORDER BY review_id DESC LIMIT 1",
            (left["job_id"], right["job_id"], right["job_id"], left["job_id"]),
        ).fetchone()
        pairs.append({"left": dict(left), "right": dict(right), "kind": conflict["kind"],
                      "similarity": conflict["similarity"], "reason": conflict["reason"],
                      "review": dict(review) if review else None})
    return sorted(pairs, key=lambda pair: (0 if pair["kind"] == "exact" else 1,
                                           -pair["similarity"], pair["left"]["job_id"]))


def review_approved_duplicate(conn, job_id: str, related_id: str, action: str,
                              note: str = "") -> None:
    if action not in ("keep_both", "mark_duplicate") or job_id == related_id:
        raise ValueError("重复审查操作无效")
    job, other = jpdb.get_job(conn, job_id), jpdb.get_job(conn, related_id)
    if not job or not other or job["status"] != Status.APPROVED or other["status"] != Status.APPROVED:
        raise ValueError("两条岗位都必须仍在已选待投递")
    conflict = compare_rows(job, other)
    if not conflict:
        raise ValueError("当前两条岗位不再符合重复审查条件")
    previous = conn.execute(
        "SELECT action FROM duplicate_reviews WHERE (job_id=? AND related_job_id=?) "
        "OR (job_id=? AND related_job_id=?) ORDER BY review_id DESC LIMIT 1",
        (job_id, related_id, related_id, job_id),
    ).fetchone()
    if action == "keep_both" and previous and previous["action"] == "keep_both":
        return
    conn.execute("INSERT INTO duplicate_reviews(job_id, related_job_id, action, note, created_at) "
                 "VALUES(?,?,?,?,?)", (job_id, related_id, action, note, jpdb.now_iso()))
    if action == "mark_duplicate":
        raw = json.loads(job["raw_json"] or "{}")
        raw["dup_of"], raw["dup_reason"] = related_id, "人工重复审查：" + conflict["reason"]
        raw["manual_duplicate"] = True
        conn.execute("UPDATE jobs SET raw_json=? WHERE job_id=?", (jpdb.json_dumps(raw), job_id))
        jpdb.set_status(conn, job_id, Status.DUPLICATE)
    else:
        conn.commit()


def queue_rows(conn) -> List[Dict[str, Any]]:
    """The board and CLI share one order; manual requeues are handled first."""
    approved_by_jd: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in conn.execute("SELECT job_id, company, title, status, jd_text FROM jobs WHERE status=?", (Status.APPROVED,)):
        key = "".join((row["jd_text"] or "").split()).casefold()
        if len(key) >= 150:
            approved_by_jd[key].append({"job_id": row["job_id"], "company": row["company"],
                                        "title": row["title"], "status": row["status"],
                                        "status_label": _STATUS_LABELS.get(row["status"], row["status"])})

    rows = [dict(row) for row in conn.execute(
        "SELECT job_id, company, title, region, source, status, prescore, url, location, jd_text, "
        "fetched_at, raw_json FROM jobs WHERE status IN (?,?)", (Status.QUEUED, Status.ANALYZED))]
    for row in rows:
        raw = json.loads(row["raw_json"] or "{}")
        row["manual_requeued"] = bool(raw.get("manual_requeued_at") or raw.get("dup_keep"))
        row["manual_requeued_at"] = raw.get("manual_requeued_at") or ""
        row["manual_priority"] = raw.get("manual_priority") if isinstance(raw.get("manual_priority"), int) else 1
        origin_id = raw.get("requeued_from_dup_of") or raw.get("dup_of")
        origin = jpdb.get_job(conn, origin_id) if origin_id else None
        row["original_representative"] = ({"job_id": origin["job_id"], "company": origin["company"],
                                           "title": origin["title"], "status": origin["status"],
                                           "status_label": _STATUS_LABELS.get(origin["status"], origin["status"])}
                                          if origin else None)
        row["original_duplicate_reason"] = raw.get("requeued_dup_reason") or raw.get("dup_reason") or ""
        key = "".join((row["jd_text"] or "").split()).casefold()
        row["same_jd_approved"] = approved_by_jd.get(key, []) if len(key) >= 150 else []
        confirmed = set(raw.get("confirmed_distinct_related_ids") or [])
        row["blocking_approved"] = [item for item in find_approved_conflicts(conn, row)
                                    if item["kind"] == "exact" and item["related_job_id"] not in confirmed]

    rows.sort(key=lambda row: (0 if row["manual_requeued"] else (1 if row["status"] == Status.ANALYZED else 2),
                               row["manual_priority"] if row["manual_requeued"] else 1,
                               row["manual_requeued_at"] if row["manual_requeued"] else "",
                               -(row["prescore"] or 0), row["job_id"]))
    for rank, row in enumerate(rows, 1):
        row["queue_rank"] = rank
    return rows


def _gaps(analysis: Dict[str, Any], match: Dict[str, Any]) -> List[str]:
    name = {r["id"]: r["requirement"] for r in analysis.get("atomic_requirements", [])}
    out = []
    for it in match.get("items", []):
        if it["verdict"] in ("partial", "no_evidence", "gap"):
            out.append("%s（%s）" % (name.get(it["req_id"], it["req_id"]), it.get("gap_type") or "-"))
    return out


def pending_rows(conn, status: str = Status.PENDING_REVIEW) -> List[Dict[str, Any]]:
    if status not in (Status.PENDING_REVIEW, Status.APPROVED):
        raise ValueError("只可列出待挑选或已选待投递岗位")
    rows = conn.execute(
        "SELECT j.job_id, j.company, j.title, j.region, j.source, j.url, j.location, j.jd_text, j.fetched_at, j.raw_json, m.soft_score, m.fixable, m.hard_fail_reasons, "
        "a.payload AS a_payload, m.payload AS m_payload FROM jobs j "
        "JOIN matches m ON m.job_id=j.job_id JOIN analyses a ON a.job_id=j.job_id "
        "WHERE j.status=? ORDER BY m.soft_score DESC, j.fetched_at DESC", (status,)).fetchall()
    out = []
    for r in rows:
        a, m = json.loads(r["a_payload"]), json.loads(r["m_payload"])
        out.append({"job_id": r["job_id"], "company": r["company"], "title": r["title"], "region": r["region"],
                    "source": r["source"], "url": r["url"], "location": r["location"], "jd_text": r["jd_text"], "fetched_at": r["fetched_at"],
                    "soft_score": r["soft_score"], "fixable": bool(r["fixable"]),
                    "hard_fail_reasons": json.loads(r["hard_fail_reasons"]), "summary": a.get("summary", ""),
                    "soft_flags": (json.loads(r["raw_json"] or "{}").get("soft_flags") or []),
                    "track": json.loads(r["raw_json"] or "{}").get("track"),
                    "top_gaps": _gaps(a, m)[:3]})
    return out


def job_detail(conn, job_id: str) -> Dict[str, Any]:
    job = jpdb.get_job(conn, job_id)
    if job is None:
        raise KeyError(job_id)
    a = conn.execute("SELECT payload FROM analyses WHERE job_id=?", (job_id,)).fetchone()
    m = conn.execute("SELECT * FROM matches WHERE job_id=?", (job_id,)).fetchone()
    analysis = json.loads(a["payload"]) if a else None
    match = json.loads(m["payload"]) if m else None
    quote_warnings = [req["id"] for req in (analysis or {}).get("atomic_requirements", [])
                      if req.get("quote") and req["quote"] not in (job["jd_text"] or "")]
    breakdown = None
    if analysis and match:
        from jp import scoring
        breakdown = scoring.explain(analysis["atomic_requirements"], match["items"], bool(m["hard_pass"]))
    raw = json.loads(job["raw_json"] or "{}")
    return {"job": dict(job), "analysis": analysis, "match": match,
            "quote_warnings": quote_warnings,
            "soft_score": m["soft_score"] if m else None, "fixable": bool(m["fixable"]) if m else None,
            "hard_fail_reasons": json.loads(m["hard_fail_reasons"]) if m else [],
            "soft_flags": raw.get("soft_flags") or [],
            "duplicate": duplicate_info(conn, raw) if job["status"] == Status.DUPLICATE else None,
            "breakdown": breakdown}


def duplicate_info(conn, raw: Dict[str, Any]) -> Dict[str, Any]:
    """Explain where a folded posting went, including the representative's current status."""
    ref = jpdb.get_job(conn, raw["dup_of"]) if raw.get("dup_of") else None
    representative = None
    if ref:
        representative = {"job_id": ref["job_id"], "company": ref["company"],
                          "title": ref["title"], "status": ref["status"],
                          "status_label": _STATUS_LABELS.get(ref["status"], ref["status"])}
    return {"reason": raw.get("dup_reason") or "与代表帖重复", "representative": representative}
