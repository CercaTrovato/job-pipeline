from __future__ import annotations
import datetime as _dt
import sqlite3
from typing import List, Optional

from jp import db as jpdb

_FMT = "%Y-%m-%dT%H:%M:%S"


def _now(now: Optional[str]) -> str:
    return now or jpdb.now_iso()


def is_cooling(conn: sqlite3.Connection, source: str, now: Optional[str] = None) -> Optional[sqlite3.Row]:
    """冷却中返回该行，否则 None（ISO 字符串可直接比较）。"""
    row = conn.execute("SELECT * FROM cooldowns WHERE source=?", (source,)).fetchone()
    if row is None or row["until"] <= _now(now):
        return None
    return row


def start_cooldown(conn: sqlite3.Connection, source: str, reason: str, hours: int = 24, now: Optional[str] = None) -> str:
    """记录冷却（覆盖旧记录），返回 until。触发原因来自 RiskControlError；绝不在此做任何绕过。"""
    start = _dt.datetime.strptime(_now(now), _FMT)
    until = (start + _dt.timedelta(hours=hours)).strftime(_FMT)
    conn.execute("INSERT OR REPLACE INTO cooldowns(source, until, reason, created_at) VALUES(?,?,?,?)",
                 (source, until, (reason or "")[:500], start.strftime(_FMT)))
    conn.commit()
    return until


def clear_cooldown(conn: sqlite3.Connection, source: str) -> None:
    conn.execute("DELETE FROM cooldowns WHERE source=?", (source,))
    conn.commit()


def active(conn: sqlite3.Connection, now: Optional[str] = None) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM cooldowns WHERE until > ? ORDER BY source", (_now(now),)).fetchall()
