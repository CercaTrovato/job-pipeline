from __future__ import annotations

import json
import threading
import uuid

from filelock import Timeout

from jp import db
from jp.service import Stopped

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_tasks(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, options TEXT NOT NULL,
 status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '', progress TEXT NOT NULL DEFAULT '{}',
 result TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, finished_at TEXT,
 stop_requested INTEGER NOT NULL DEFAULT 0
);
"""
KINDS = ("fetch", "analyze", "pipeline", "resume_generate")


class TaskRunner:
    def __init__(self, service):
        self.service = service
        with service.connect() as conn:
            conn.executescript(SCHEMA)
        try:
            with service.lock:
                with service.connect() as conn:
                    conn.execute("UPDATE workspace_tasks SET status='interrupted',finished_at=? WHERE status='running'",
                                 (db.now_iso(),))
        except Timeout:
            pass

    def list(self):
        conn = self.service.connect()
        try:
            result = []
            for row in conn.execute("SELECT * FROM workspace_tasks ORDER BY created_at DESC,rowid DESC LIMIT 30"):
                item = dict(row)
                for key in ("options", "progress", "result"):
                    item[key] = json.loads(item[key])
                result.append(item)
            return result
        finally:
            conn.close()

    def start(self, kind, options=None):
        if kind not in KINDS:
            raise ValueError("不支持的任务类型。")
        options = options or {}
        if set(options) - {"region", "sources", "max_jobs", "job_ids"}:
            raise ValueError("未知任务选项。")
        if options.get("region", "CN") not in ("CN", "HK"):
            raise ValueError("地区必须为 CN/HK。")
        if "sources" in options and (not isinstance(options["sources"], list) or any(s not in (
                "boss", "nowcoder", "linkedin", "jobsdb", "watchlist") for s in options["sources"])):
            raise ValueError("岗位来源不合法。")
        if "max_jobs" in options and (not isinstance(options["max_jobs"], int) or isinstance(options["max_jobs"], bool)
                                     or not 1 <= options["max_jobs"] <= 100):
            raise ValueError("岗位预算必须为 1–100。")
        if "job_ids" in options and (not isinstance(options["job_ids"], list) or any(
                not isinstance(jid, str) or not jid.isalnum() or len(jid) > 64 for jid in options["job_ids"])):
            raise ValueError("岗位 ID 不合法。")
        # 同一 runner 对象的 FileLock 可重入；每次任务必须使用新的锁实例。
        from filelock import FileLock
        lock = FileLock(str(self.service.workspace.root / ".pipeline.lock"), timeout=0, thread_local=False)
        lock.acquire()
        tid = uuid.uuid4().hex
        try:
            with self.service.connect() as conn:
                conn.execute("INSERT INTO workspace_tasks(id,kind,options,status,created_at) VALUES(?,?,?,'running',?)",
                             (tid, kind, db.json_dumps(options), db.now_iso()))
            thread = threading.Thread(target=self._execute, args=(tid, kind, options, lock), daemon=True)
            thread.start()
        except BaseException:
            try:
                with self.service.connect() as conn:
                    conn.execute("UPDATE workspace_tasks SET status='failed',finished_at=?,result=? WHERE id=?",
                                 (db.now_iso(), db.json_dumps({"message": "后台线程未能启动，请稍后重试。"}), tid))
            finally:
                lock.release()
            raise
        return tid

    def stop(self, tid):
        with self.service.connect() as conn:
            changed = conn.execute("UPDATE workspace_tasks SET stop_requested=1 WHERE id=? AND status='running'", (tid,))
            if changed.rowcount != 1:
                raise ValueError("任务不存在或已经结束。")

    def resume(self, tid):
        with self.service.connect() as conn:
            found = conn.execute("SELECT * FROM workspace_tasks WHERE id=?", (tid,)).fetchone()
            task = dict(found) if found else None
        if not task or task["status"] not in ("interrupted", "stopped", "failed", "waiting_manual"):
            raise ValueError("此任务无需恢复。")
        return self.start(task["kind"], json.loads(task["options"]))

    def _execute(self, tid, kind, options, lock):
        def stopped():
            conn = self.service.connect()
            try:
                row = conn.execute("SELECT stop_requested FROM workspace_tasks WHERE id=?", (tid,)).fetchone()
                return bool(row[0])
            finally:
                conn.close()

        def stage(name, progress):
            with self.service.connect() as conn:
                conn.execute("UPDATE workspace_tasks SET stage=?,progress=? WHERE id=?", (name, db.json_dumps(progress), tid))

        status, result = "done", {}
        try:
            result = self.service.run(kind, options, stopped, stage)
            status = "waiting_manual" if result.get("manual") else "stopped" if stopped() else "done"
        except Stopped:
            status = "stopped"
        except Exception as exc:
            from jp.security import safe_error
            status, result = "failed", {"message": safe_error(exc)}
        finally:
            try:
                with self.service.connect() as conn:
                    conn.execute("UPDATE workspace_tasks SET status=?,result=?,finished_at=? WHERE id=?",
                                 (status, db.json_dumps(result), db.now_iso(), tid))
            finally:
                lock.release()
