from __future__ import annotations
from typing import Dict, List

CREDIT = {"strong": 1.0, "exceeds": 1.0, "partial": 0.5, "no_evidence": 0.15, "gap": 0.0}
WEIGHT = {"high": 0.6, "mid": 0.3, "low": 0.1}
HARD_FAIL_CAP = 30.0
GAP_VERDICTS = {"partial", "no_evidence", "gap"}


def soft_score(atomic_requirements: List[Dict], items: List[Dict], hard_pass: bool) -> float:
    level_of = {r["id"]: r["level"] for r in atomic_requirements}
    buckets: Dict[str, List[float]] = {"high": [], "mid": [], "low": []}
    for it in items:
        lv = level_of.get(it["req_id"])
        if lv in buckets:
            buckets[lv].append(CREDIT[it["verdict"]])
    present = {lv: vals for lv, vals in buckets.items() if vals}
    if not present:
        return 0.0
    total_w = sum(WEIGHT[lv] for lv in present)
    score = 100.0 * sum((sum(v) / len(v)) * (WEIGHT[lv] / total_w) for lv, v in present.items())
    if not hard_pass:
        score = min(score, HARD_FAIL_CAP)
    return round(score, 1)


def fixable(items: List[Dict]) -> bool:
    gaps = [it for it in items if it["verdict"] in GAP_VERDICTS]
    return all(it.get("gap_type") == "expression" for it in gaps)


LEVEL_NAME = {"high": "高", "mid": "中", "low": "低"}


def explain(atomic_requirements: List[Dict], items: List[Dict], hard_pass: bool) -> Dict:
    """把 soft_score 的计算过程展开成可展示的明细（供看板/CLI 用），数值与 soft_score 完全一致。"""
    level_of = {r["id"]: r["level"] for r in atomic_requirements}
    buckets: Dict[str, List[Dict]] = {"high": [], "mid": [], "low": []}
    for it in items:
        lv = level_of.get(it["req_id"])
        if lv in buckets:
            buckets[lv].append({"req_id": it["req_id"], "verdict": it["verdict"], "credit": CREDIT[it["verdict"]]})
    present = [lv for lv in ("high", "mid", "low") if buckets[lv]]
    total_w = sum(WEIGHT[lv] for lv in present) or 1.0
    rows = []
    raw = 0.0
    for lv in present:
        credits = [x["credit"] for x in buckets[lv]]
        mean = sum(credits) / len(credits)
        w = WEIGHT[lv] / total_w
        contrib = 100.0 * mean * w
        raw += contrib
        rows.append({"level": lv, "level_name": LEVEL_NAME[lv], "count": len(credits), "mean": round(mean, 2),
                     "weight": round(w, 2), "contribution": round(contrib, 1), "items": buckets[lv]})
    capped = (not hard_pass) and raw > HARD_FAIL_CAP
    return {"rows": rows, "raw": round(raw, 1), "hard_pass": hard_pass, "cap": HARD_FAIL_CAP, "capped": capped,
            "score": round(min(raw, HARD_FAIL_CAP) if not hard_pass else raw, 1),
            "credit_table": CREDIT, "weight_table": WEIGHT}
