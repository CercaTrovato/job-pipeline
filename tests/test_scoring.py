from __future__ import annotations
from jp import scoring

def req(i, level):
    return {"id": "R%d" % i, "quote": "q", "requirement": "r", "level": level, "kind": "skill"}

def item(i, verdict, gap=None, grade="B"):
    return {"req_id": "R%d" % i, "evidence_grade": grade, "fact_ids": [], "verdict": verdict, "gap_type": gap, "note": ""}

def test_all_strong_is_100():
    reqs = [req(1, "high"), req(2, "mid"), req(3, "low")]
    items = [item(1, "strong"), item(2, "exceeds"), item(3, "strong")]
    assert scoring.soft_score(reqs, items, True) == 100.0

def test_weights_redistribute_when_level_missing():
    reqs = [req(1, "high"), req(2, "high")]
    items = [item(1, "strong"), item(2, "gap", "ability")]
    assert scoring.soft_score(reqs, items, True) == 50.0

def test_mixed_levels():
    reqs = [req(1, "high"), req(2, "mid")]
    items = [item(1, "partial", "expression"), item(2, "strong")]
    # high: 0.5 * (0.6/0.9) + mid: 1.0 * (0.3/0.9) = 0.3333 + 0.3333 = 66.7
    assert scoring.soft_score(reqs, items, True) == 66.7

def test_hard_fail_caps_at_30():
    reqs = [req(1, "high")]
    assert scoring.soft_score(reqs, [item(1, "strong")], False) == 30.0

def test_fixable():
    assert scoring.fixable([item(1, "strong")]) is True
    assert scoring.fixable([item(1, "partial", "expression"), item(2, "strong")]) is True
    assert scoring.fixable([item(1, "gap", "ability")]) is False
    assert scoring.fixable([item(1, "no_evidence", "evidence")]) is False


def test_explain_matches_soft_score_and_shows_cap():
    reqs = [req(1, "high"), req(2, "high"), req(3, "mid")]
    items = [item(1, "strong"), item(2, "partial", "expression"), item(3, "exceeds")]
    ex = scoring.explain(reqs, items, True)
    assert ex["score"] == scoring.soft_score(reqs, items, True) == 83.3
    assert [r["level"] for r in ex["rows"]] == ["high", "mid"] and ex["rows"][0]["count"] == 2 and ex["rows"][0]["mean"] == 0.75
    assert round(sum(r["contribution"] for r in ex["rows"]), 1) == ex["raw"]
    ex2 = scoring.explain(reqs, items, False)
    assert ex2["capped"] is True and ex2["score"] == 30.0 and ex2["raw"] == 83.3
