from __future__ import annotations
from jp import sentinel


def test_no_cooldown_by_default(conn):
    assert sentinel.is_cooling(conn, "boss") is None
    assert sentinel.active(conn) == []


def test_start_then_is_cooling_until_expiry(conn):
    until = sentinel.start_cooldown(conn, "boss", "疑似风控：安全验证", hours=24, now="2026-09-18T22:00:00")
    assert until == "2026-09-19T22:00:00"
    row = sentinel.is_cooling(conn, "boss", now="2026-09-19T21:59:59")
    assert row is not None and row["reason"] == "疑似风控：安全验证" and row["until"] == until
    assert sentinel.is_cooling(conn, "boss", now="2026-09-19T22:00:00") is None
    assert [r["source"] for r in sentinel.active(conn, now="2026-09-19T00:00:00")] == ["boss"]
    assert sentinel.active(conn, now="2026-09-20T00:00:00") == []


def test_restart_overwrites_and_clear_removes(conn):
    sentinel.start_cooldown(conn, "linkedin", "a", hours=1, now="2026-09-18T22:00:00")
    sentinel.start_cooldown(conn, "linkedin", "b", hours=2, now="2026-09-18T22:00:00")
    row = sentinel.is_cooling(conn, "linkedin", now="2026-09-18T23:30:00")
    assert row is not None and row["reason"] == "b" and row["until"] == "2026-09-19T00:00:00"
    sentinel.clear_cooldown(conn, "linkedin")
    assert sentinel.is_cooling(conn, "linkedin", now="2026-09-18T22:00:01") is None
