from __future__ import annotations
import datetime as dt
import json
import pytest
from jp import decide, ingest
from jp import db as jpdb
from jp.models import RawJob, Status

def _pending(conn, pid="p1", score=80.0, gaps=None):
    r = ingest.ingest_jobs(conn, [RawJob(platform_id=pid, title="T" + pid, company="C", url="u", jd_text="jd", location="深圳")], "boss", "CN")
    jid = r.job_ids[0]
    jpdb.set_status(conn, jid, Status.PENDING_REVIEW)
    analysis = {"summary": "摘要 " + pid, "atomic_requirements": [{"id": "R1", "quote": "q", "requirement": "Python", "level": "high", "kind": "skill"}],
                "keywords": {"must": ["Python"], "core": [], "bonus": []}, "hard_conditions": {}, "flags": {}}
    items = [{"req_id": "R1", "evidence_grade": "E", "fact_ids": [], "verdict": "gap", "gap_type": g, "note": "n"} for g in (gaps or [])] or \
            [{"req_id": "R1", "evidence_grade": "A", "fact_ids": ["x"], "verdict": "strong", "gap_type": None, "note": ""}]
    conn.execute("INSERT INTO analyses VALUES(?,?,?,?,?)", (jid, "h", json.dumps(analysis, ensure_ascii=False), "packet", jpdb.now_iso()))
    conn.execute("INSERT INTO matches VALUES(?,?,?,?,?,?,?,?)",
                 (jid, "h", 1, "[]", json.dumps({"items": items, "rationale": "r"}, ensure_ascii=False), score, 1, jpdb.now_iso()))
    conn.commit()
    return jid

def test_apply_and_status(conn):
    jid = _pending(conn)
    assert decide.apply(conn, jid, "apply", "看起来不错") == Status.APPROVED
    d = conn.execute("SELECT decision, note FROM decisions WHERE job_id=?", (jid,)).fetchone()
    assert (d["decision"], d["note"]) == ("apply", "看起来不错")

def test_later_then_release(conn):
    jid = _pending(conn)
    decide.apply(conn, jid, "later", later_days=0)
    assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (jid,)).fetchone()[0] == Status.LATER
    assert decide.release_later(conn) == 1
    assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (jid,)).fetchone()[0] == Status.PENDING_REVIEW

def test_only_pending_review_can_be_decided(conn):
    jid = _pending(conn)
    decide.apply(conn, jid, "skip")
    with pytest.raises(ValueError):
        decide.apply(conn, jid, "apply")

def test_pending_rows_sorted_with_gaps(conn):
    a = _pending(conn, "a", 60.0, gaps=["ability"])
    b = _pending(conn, "b", 90.0)
    rows = decide.pending_rows(conn)
    assert [r["job_id"] for r in rows] == [b, a]
    assert rows[1]["top_gaps"] == ["Python（ability）"] and rows[0]["summary"] == "摘要 b"

def test_requeue(conn):
    r = ingest.ingest_jobs(conn, [RawJob(platform_id="z", title="T", company="C", url="u", jd_text="jd")], "boss", "CN")
    jpdb.set_status(conn, r.job_ids[0], Status.PRESCREENED_OUT)
    decide.requeue(conn, r.job_ids[0])
    assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (r.job_ids[0],)).fetchone()[0] == Status.QUEUED
