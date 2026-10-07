from __future__ import annotations
import datetime as _dt
import json
import pathlib
import sqlite3
from typing import Any, Dict, Optional

import yaml

from jp.models import Status


def now_iso() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def connect(path, factory=sqlite3.Connection) -> sqlite3.Connection:
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, factory=factory)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection, schema_path) -> None:
    with open(schema_path, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()


def set_status(conn: sqlite3.Connection, job_id: str, new_status: str) -> None:
    if new_status not in Status.ALL:
        raise ValueError("未知状态: %s" % new_status)
    cur = conn.execute("UPDATE jobs SET status=? WHERE job_id=?", (new_status, job_id))
    if cur.rowcount != 1:
        raise KeyError("job 不存在: %s" % job_id)
    conn.commit()


def get_job(conn: sqlite3.Connection, job_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()


def jobs_by_status(conn: sqlite3.Connection, status: str):
    return conn.execute("SELECT * FROM jobs WHERE status=? ORDER BY fetched_at DESC", (status,)).fetchall()


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


_EDITABLE = ("company", "title", "location", "url")


def update_job_fields(conn: sqlite3.Connection, job_id: str, **fields: Any) -> None:
    bad = [k for k in fields if k not in _EDITABLE]
    if bad:
        raise ValueError("不允许修改的字段: %s" % bad)
    if not fields:
        return
    sets = ", ".join("%s=?" % k for k in fields)
    cur = conn.execute("UPDATE jobs SET %s WHERE job_id=?" % sets, (*fields.values(), job_id))
    if cur.rowcount != 1:
        raise KeyError("job 不存在: %s" % job_id)
    conn.commit()
