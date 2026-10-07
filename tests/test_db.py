from __future__ import annotations
from jp import db as jpdb
from jp.models import Status

def test_init_creates_tables(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"jobs", "job_sources", "analyses", "matches", "decisions", "applications", "runs"} <= names

def test_set_status_rejects_unknown(conn):
    conn.execute(
        "INSERT INTO jobs(job_id,source,region,company,title,jd_text,fetched_at,last_seen_at,fingerprint) "
        "VALUES('j1','referral','CN','A','B','jd','2026-09-15T00:00:00','2026-09-15T00:00:00','fp')"
    )
    jpdb.set_status(conn, "j1", Status.QUEUED)
    assert conn.execute("SELECT status FROM jobs WHERE job_id='j1'").fetchone()[0] == "queued"
    import pytest
    with pytest.raises(ValueError):
        jpdb.set_status(conn, "j1", "bogus")
