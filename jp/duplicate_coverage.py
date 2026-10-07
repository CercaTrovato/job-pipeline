"""只读审计重复簇覆盖，并有限晋升可信且符合硬条件的代表帖。"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from jp import db as jpdb
from jp import selection_duplicates
from jp.models import Status
from jp.rules import hard

_STATUS_LABELS = {
    Status.APPROVED: "已选待投递", Status.SKIPPED: "已跳过", Status.LATER: "稍后处理",
    Status.PENDING_REVIEW: "待挑选", Status.QUEUED: "待分析", Status.ANALYZED: "已分析待匹配",
    Status.FETCHED: "候补", Status.PRESCREENED_OUT: "粗筛未入队", Status.REJECTED_HARD: "硬条件淘汰",
    Status.DUPLICATE: "重复帖",
}
_TECH_ROLE = re.compile(r"(?<![a-z])AI(?![a-z])|人工智能|算法|数据|后端|软件开发|机器学习|大模型|backend|software|machine learning", re.I)
_NONTECH_ROLE = re.compile(r"产品|运营|商务|销售|人事|行政", re.I)


def _raw(row: Any) -> Dict[str, Any]:
    try:
        value = row["raw_json"]
    except (KeyError, IndexError, TypeError):
        value = ""
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _analysis(conn: Any, job_id: str) -> Dict[str, Any]:
    row = conn.execute("SELECT payload FROM analyses WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        return {}
    try:
        payload = json.loads(row["payload"] if hasattr(row, "keys") else row[0])
        return payload if isinstance(payload, dict) else {}
    except (ValueError, TypeError, IndexError):
        return {}


def _hard_result(conn: Any, row: Any, constraints: Dict[str, Any]):
    analysis = _analysis(conn, row["job_id"])
    return hard.check_job(row, constraints, analysis.get("hard_conditions"), analysis.get("flags"))


def _trusted_source(row: Any) -> bool:
    source = str(row["source"] or "").casefold()
    raw = _raw(row)
    if source == "watchlist":
        # 正式 watchlist 适配器会写入 ATS 元数据；仅来源标签不足以证明是官方入口。
        info = raw.get("watchlist") or {}
        return bool(info.get("ats") and info.get("company"))
    if source != "boss":
        return False
    detail = raw.get("detail") or {}
    detail_company = str(detail.get("company") or "").strip()
    description = str(detail.get("description") or "").strip()
    if not detail_company or not description:
        return False
    return selection_duplicates.brand_key(detail_company) == selection_duplicates.brand_key(str(row["company"] or ""))


def _approved_exact_exists(conn: Any, candidate: Any) -> bool:
    rows = conn.execute(
        "SELECT job_id, source, region, company, title, location, url, jd_text, raw_json "
        "FROM jobs WHERE status=? AND job_id<>?",
        (Status.APPROVED, candidate["job_id"]),
    ).fetchall()
    for approved in rows:
        conflict = selection_duplicates.compare_rows(candidate, approved)
        if conflict and conflict.get("kind") == "exact":
            return True
    return False


def _eligibility(conn: Any, row: Any, constraints: Dict[str, Any]) -> str:
    if row["status"] not in (Status.FETCHED, Status.PRESCREENED_OUT):
        return ""
    if not _trusted_source(row):
        return "来源未达到自动晋升可信标准；跨公司模板不自动恢复"
    title = str(row["title"] or "")
    if not _TECH_ROLE.search(title) or _NONTECH_ROLE.search(title):
        return "岗位标题不是明确的 AI、技术或数据岗位，或属于产品/运营/商务"
    if float(row["prescore"] or 0) < 30:
        return "粗筛分低于 30 分"
    result = _hard_result(conn, row, constraints)
    if not result.passed:
        return "硬条件不符合：" + "；".join(result.reasons)
    if _approved_exact_exists(conn, row):
        return "同品牌已有完全重复的已选岗位"
    return "符合自动晋升条件（硬条件通过、粗筛分≥30、岗位与来源可信）"


def audit_clusters(conn: Any, constraints: Dict[str, Any]) -> List[dict]:
    """按 duplicate.raw_json.dup_of 分组，报告代表帖当前状态、数量、原因与晋升资格。"""
    rows = conn.execute(
        "SELECT job_id, source, region, company, title, status, prescore, location, url, jd_text, raw_json "
        "FROM jobs ORDER BY job_id"
    ).fetchall()
    by_id = {str(row["job_id"]): row for row in rows}
    child_ids: Dict[str, List[str]] = {}
    for row in rows:
        if row["status"] != Status.DUPLICATE:
            continue
        representative_id = str(_raw(row).get("dup_of") or "")
        if representative_id:
            child_ids.setdefault(representative_id, []).append(str(row["job_id"]))

    result: List[dict] = []
    for representative_id, children in child_ids.items():
        representative = by_id.get(representative_id)
        if representative is None:
            result.append({
                "job_id": representative_id, "company": "", "title": "", "status": "missing",
                "status_label": "代表帖不存在", "children": children, "children_count": len(children),
                "reason": "重复帖指向的代表记录不存在", "eligible": False,
            })
            continue
        status = representative["status"]
        eligible = False
        if status in (Status.APPROVED, Status.SKIPPED, Status.LATER, Status.PENDING_REVIEW):
            reason = "代表帖已有人工作出选择或正在待挑选"
        elif status in (Status.QUEUED, Status.ANALYZED):
            reason = "代表帖已在待处理流程中"
        elif status == Status.REJECTED_HARD:
            hard_result = _hard_result(conn, representative, constraints)
            reasons = hard_result.reasons or _raw(representative).get("prescreen_reasons") or []
            reason = ("硬条件不符合：" + "；".join(reasons)) if reasons else "历史硬条件淘汰记录；当前规则未复现明确原因"
        elif status in (Status.FETCHED, Status.PRESCREENED_OUT):
            reason = _eligibility(conn, representative, constraints)
            eligible = reason.startswith("符合自动晋升条件")
        else:
            reason = "代表帖状态为 %s，未自动恢复" % status
        result.append({
            "job_id": representative_id,
            "company": representative["company"],
            "title": representative["title"],
            "status": status,
            "status_label": _STATUS_LABELS.get(status, status),
            "children": children,
            "children_count": len(children),
            "reason": reason,
            "eligible": eligible,
        })
    result.sort(key=lambda item: (not item["eligible"], item["company"], item["title"], item["job_id"]))
    return result


def promote_eligible_representatives(conn: Any, constraints: Dict[str, Any], max_new: int = 3) -> List[str]:
    """把最多 max_new 条符合条件的 fetched/prescreened_out 代表帖推进 queued。"""
    if max_new <= 0:
        return []
    audit = audit_clusters(conn, constraints)
    promoted: List[str] = []
    for item in audit:
        if len(promoted) >= max_new:
            break
        if not item["eligible"]:
            continue
        jpdb.set_status(conn, item["job_id"], Status.QUEUED)
        promoted.append(item["job_id"])
    return promoted
