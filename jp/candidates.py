from __future__ import annotations
from typing import Dict, Iterable, List, Set

from jp import facts_index as fi

_GRADE_ORDER = {"A": 0, "A/B": 0, "B": 1, "C": 2, "D": 3}


def _grade_rank(g: str) -> int:
    g = (g or "").strip().upper()
    for k, v in _GRADE_ORDER.items():
        if g.startswith(k):
            return v
    return 4


def candidate_facts(requirements: List[dict], entries: List[dict], k: int = 5) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    kw_of = {e["id"]: set(e["keywords"]) for e in entries}
    grade_of = {e["id"]: e.get("grade", "") for e in entries}
    for r in requirements:
        toks = set(fi.tokenize("%s %s" % (r.get("requirement", ""), r.get("quote", ""))))
        scored = []
        for fid, kws in kw_of.items():
            n = len(toks & kws)
            if n:
                scored.append((-n, _grade_rank(grade_of[fid]), fid))
        scored.sort()
        out[r["id"]] = [fid for _, _, fid in scored[:k]]
    return out


def brief(entries: List[dict], ids: Iterable[str]) -> List[dict]:
    want: Set[str] = set(ids)
    return [{k: e[k] for k in ("id", "type", "title", "text", "grade")} for e in sorted(entries, key=lambda e: e["id"]) if e["id"] in want]
