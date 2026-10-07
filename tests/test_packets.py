from __future__ import annotations
import json
import pathlib
import pytest
from jp import packets

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "llm" / "schemas" / "analyze.v1.json"

GOOD = {
    "atomic_requirements": [{"id": "R1", "quote": "熟悉 Python", "requirement": "Python 编程", "level": "high", "kind": "skill"}],
    "keywords": {"must": ["Python"], "core": [], "bonus": []},
    "hard_conditions": {"location": ["深圳"], "days_per_week": 4, "min_months": 3, "employment_type": "internship",
                        "graduated_required": False, "onsite_days": None, "visa": "unknown", "language": []},
    "flags": {"entry_level": True, "remote": False, "full_time": False},
    "summary": "深圳大模型应用实习，要求 Python。",
}

def test_create_writes_four_files(tmp_path):
    p = packets.create(tmp_path, "j1", "analyze", {"jd_text": "x"}, "PROMPT", SCHEMA)
    assert {f.name for f in p.dir.iterdir()} == {"input.json", "prompt.md", "schema.json"}
    assert packets.status(p) == "pending"
    assert p.input_hash == packets.input_hash({"jd_text": "x"})

def test_create_idempotent_keeps_output(tmp_path):
    p = packets.create(tmp_path, "j1", "analyze", {"jd_text": "x"}, "PROMPT", SCHEMA)
    (p.dir / "output.json").write_text(json.dumps(GOOD, ensure_ascii=False), encoding="utf-8")
    p2 = packets.create(tmp_path, "j1", "analyze", {"jd_text": "x"}, "PROMPT-changed", SCHEMA)
    assert (p2.dir / "output.json").exists() and packets.status(p2) == "done"

def test_create_new_input_rebuilds(tmp_path):
    p = packets.create(tmp_path, "j1", "analyze", {"jd_text": "x"}, "PROMPT", SCHEMA)
    (p.dir / "output.json").write_text("{}", encoding="utf-8")
    p2 = packets.create(tmp_path, "j1", "analyze", {"jd_text": "y"}, "PROMPT", SCHEMA)
    assert not (p2.dir / "output.json").exists()

def test_validate_good_and_bad(tmp_path):
    p = packets.create(tmp_path, "j1", "analyze", {"jd_text": "x"}, "PROMPT", SCHEMA)
    (p.dir / "output.json").write_text(json.dumps(GOOD, ensure_ascii=False), encoding="utf-8")
    assert packets.validate(p)["summary"].startswith("深圳")
    bad = dict(GOOD); bad["summary"] = "短"
    (p.dir / "output.json").write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(packets.PacketError):
        packets.validate(p)
    assert (p.dir / "error.txt").exists() and packets.status(p) == "error"

def test_list_pending(tmp_path):
    packets.create(tmp_path, "j1", "analyze", {"a": 1}, "P", SCHEMA)
    p2 = packets.create(tmp_path, "j2", "analyze", {"a": 2}, "P", SCHEMA)
    (p2.dir / "output.json").write_text(json.dumps(GOOD, ensure_ascii=False), encoding="utf-8")
    pend = packets.list_pending(tmp_path, "analyze")
    assert [p.job_id for p in pend] == ["j1"]

def test_list_pending_includes_error_packets(tmp_path):
    p = packets.create(tmp_path, "j1", "analyze", {"a": 1}, "P", SCHEMA)
    (p.dir / "output.json").write_text("{", encoding="utf-8")
    with pytest.raises(packets.PacketError):
        packets.validate(p)
    pend = packets.list_pending(tmp_path, "analyze")
    assert [pp.job_id for pp in pend] == ["j1"]

    (p.dir / "output.json").write_text(json.dumps(GOOD, ensure_ascii=False), encoding="utf-8")
    packets.validate(p)
    assert not (p.dir / "error.txt").exists()
    assert packets.status(p) == "done"
    pend2 = packets.list_pending(tmp_path, "analyze")
    assert [pp.job_id for pp in pend2] == []

def test_validate_missing_schema_raises_packet_error(tmp_path):
    p = packets.create(tmp_path, "j1", "analyze", {"a": 1}, "P", SCHEMA)
    (p.dir / "output.json").write_text(json.dumps(GOOD, ensure_ascii=False), encoding="utf-8")
    (p.dir / "schema.json").unlink()
    with pytest.raises(packets.PacketError):
        packets.validate(p)
    assert (p.dir / "error.txt").read_text(encoding="utf-8") == "schema.json 缺失"
    assert packets.status(p) == "done"   # output 仍在，collect 会再次校验并再次报错


def test_refilled_output_after_error_is_done_again(tmp_path):
    p = packets.create(tmp_path, "j1", "analyze", {"a": 1}, "P", SCHEMA)
    (p.dir / "output.json").write_text("{", encoding="utf-8")
    with pytest.raises(packets.PacketError):
        packets.validate(p)
    assert packets.status(p) == "error"
    (p.dir / "output.json").write_text(json.dumps(GOOD, ensure_ascii=False), encoding="utf-8")
    assert packets.status(p) == "done"
    packets.validate(p)
    assert not (p.dir / "error.txt").exists() and packets.list_pending(tmp_path, "analyze") == []
