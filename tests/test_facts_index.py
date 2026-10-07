from __future__ import annotations
import json
import pathlib
from jp import facts_index as fi

FX = pathlib.Path(__file__).resolve().parent / "fixtures" / "facts"

def test_build_types_and_ids():
    entries = fi.build(FX)
    types = {e["type"] for e in entries}
    assert types == {"skill", "project", "experience", "research", "education", "award", "language"}
    ids = {e["id"] for e in entries}
    assert {"skill.programming", "demo.tiers", "bank.rag", "p1.eng", "edu.msc", "award.2025.一等奖学金", "language"} <= ids

def test_keywords_extracted():
    e = {x["id"]: x for x in fi.build(FX)}
    assert "python" in e["skill.programming"]["keywords"]
    assert "playwright" in e["demo.tiers"]["keywords"]
    assert "pytorch" in e["p1.eng"]["keywords"]

def test_no_privacy_leak(tmp_path):
    entries = fi.build(FX)
    fi.write(entries, tmp_path / "idx.json")
    raw = (tmp_path / "idx.json").read_text(encoding="utf-8")
    assert "13800000000" not in raw and "x@example.com" not in raw and "张三" not in raw
    assert fi.load(tmp_path / "idx.json") == entries

def test_tokenize_mixed():
    toks = fi.tokenize("熟悉 RAG / 知识库问答，PyTorch 训练")
    assert "rag" in toks and "pytorch" in toks and "知识库" in toks

def test_nested_items_indexed():
    e = {x["id"]: x for x in fi.build(FX)}
    assert "preprints.1" in e and "preprints.2" in e
    assert e["preprints.1"]["type"] == "research"
    assert e["preprints.2"]["type"] == "research"
    assert "graph" in e["preprints.1"]["keywords"]
    assert "arXiv 0000.00001" in e["preprints.1"]["text"]
    assert "preprints" not in e
