from __future__ import annotations
import pathlib
from jp import candidates
from jp import facts_index as fi

FX = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "facts"

def req(i, text, quote=""):
    return {"id": "R%d" % i, "requirement": text, "quote": quote or text, "level": "high", "kind": "skill"}

def test_candidate_facts_ranks_by_overlap():
    entries = fi.build(FX)
    c = candidates.candidate_facts([req(1, "Python 与 SQL 编程"), req(2, "焊接")], entries, k=3)
    assert c["R1"][0] == "skill.programming" and len(c["R1"]) <= 3
    assert c["R2"] == []

def test_brief_keeps_order_and_fields():
    entries = fi.build(FX)
    b = candidates.brief(entries, {"skill.programming", "demo.tiers"})
    assert [x["id"] for x in b] == ["demo.tiers", "skill.programming"]
    assert set(b[0]) == {"id", "type", "title", "text", "grade"}
