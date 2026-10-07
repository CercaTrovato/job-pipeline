from __future__ import annotations
from typing import Optional

from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

from jp import db as jpdb


class Run:
    def __init__(self, conn, command: str, source: Optional[str] = None, quiet: bool = False):
        self.conn, self.command, self.source, self.quiet = conn, command, source, quiet
        self.run_id: Optional[int] = None
        self._final: Optional[str] = None
        self._bar: Optional[Progress] = None
        self._task = None
        self.pages_total = 0
        self.packets_total = 0

    # ---- lifecycle ----
    def __enter__(self) -> "Run":
        cur = self.conn.execute(
            "INSERT INTO runs(command, source, started_at, status) VALUES(?,?,?,'running')",
            (self.command, self.source, jpdb.now_iso()),
        )
        self.run_id = cur.lastrowid
        self.conn.commit()
        if not self.quiet:
            self._bar = Progress(TextColumn("[bold]{task.description}"), BarColumn(), TextColumn("{task.completed}/{task.total}"),
                                 TextColumn("{task.fields[extra]}"), TimeElapsedColumn())
            self._bar.start()
            self._task = self._bar.add_task("%s %s" % (self.command, self.source or ""), total=1, extra="")
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is not None and self._final is None:
            self.fail(str(exc))
        status = self._final or "done"
        self.conn.execute("UPDATE runs SET status=?, finished_at=? WHERE run_id=?", (status, jpdb.now_iso(), self.run_id))
        self.conn.commit()
        if self._bar:
            self._bar.stop()
        return False

    # ---- updates ----
    def set_total(self, pages: Optional[int] = None, packets: Optional[int] = None) -> None:
        if pages is not None:
            self.pages_total = pages
            self.conn.execute("UPDATE runs SET pages_total=? WHERE run_id=?", (pages, self.run_id))
        if packets is not None:
            self.packets_total = packets
            self.conn.execute("UPDATE runs SET packets_total=? WHERE run_id=?", (packets, self.run_id))
        self.conn.commit()
        if self._bar:
            self._bar.update(self._task, total=max(1, self.pages_total or self.packets_total))

    def tick(self, pages: int = 0, jobs_new: int = 0, jobs_seen: int = 0, packets: int = 0) -> None:
        self.conn.execute(
            "UPDATE runs SET pages_done=pages_done+?, jobs_new=jobs_new+?, jobs_seen=jobs_seen+?, packets_done=packets_done+? WHERE run_id=?",
            (pages, jobs_new, jobs_seen, packets, self.run_id),
        )
        self.conn.commit()
        if self._bar:
            row = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (self.run_id,)).fetchone()
            self._bar.update(self._task, completed=row["pages_done"] or row["packets_done"],
                             extra="新增 %d 已见 %d" % (row["jobs_new"], row["jobs_seen"]))

    def _record_error(self, status: str, msg: str) -> None:
        self._final = status
        msg = _scrub_secrets(msg)[:500]
        self.conn.execute("UPDATE runs SET last_error=? WHERE run_id=?", (msg, self.run_id))
        self.conn.commit()

    def fail(self, msg: str) -> None:
        self._record_error("error", msg)

    def cooldown(self, msg: str) -> None:
        self._record_error("cooldown", msg)


def _scrub_secrets(msg: str) -> str:
    """runs.last_error 会显示在看板上：抹掉任何像密钥的东西（环境变量与 codex auth 里的值，以及 sk-… 形式）。"""
    import os
    import re
    for k in (os.environ.get("JP_LLM_API_KEY"),):
        if k:
            msg = msg.replace(k, "***")
    try:
        from jp import backends
        k = backends._load_key()
        if k:
            msg = msg.replace(k, "***")
    except Exception:  # noqa: BLE001 —— 读不到密钥文件就跳过
        pass
    return re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-***", msg)


def recent(conn, n: int = 10):
    return conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (n,)).fetchall()
