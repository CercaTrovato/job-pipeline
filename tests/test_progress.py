from __future__ import annotations
import pytest
from jp import progress

def test_run_records_done(conn):
    with progress.Run(conn, "prescore", quiet=True) as run:
        run.set_total(packets=3)
        run.tick(packets=2)
        run.tick(packets=1, jobs_new=5)
    row = progress.recent(conn, 1)[0]
    assert row["command"] == "prescore" and row["status"] == "done"
    assert (row["packets_done"], row["packets_total"], row["jobs_new"]) == (3, 3, 5)
    assert row["finished_at"]

def test_run_records_error_on_exception(conn):
    with pytest.raises(RuntimeError):
        with progress.Run(conn, "fetch", source="boss", quiet=True):
            raise RuntimeError("boom")
    row = progress.recent(conn, 1)[0]
    assert row["status"] == "error" and "boom" in row["last_error"] and row["source"] == "boss"

def test_cooldown(conn):
    with progress.Run(conn, "fetch", source="linkedin", quiet=True) as run:
        run.cooldown("触发验证码")
    assert progress.recent(conn, 1)[0]["status"] == "cooldown"


def test_last_error_is_scrubbed(conn, monkeypatch):
    monkeypatch.setenv("JP_LLM_API_KEY", "sk-from-env-123456")
    with pytest.raises(RuntimeError):
        with progress.Run(conn, "api", quiet=True):
            raise RuntimeError("HTTP 401 for key sk-from-env-123456 and sk-other-abcdefgh")
    err = progress.recent(conn, 1)[0]["last_error"]
    assert "sk-from-env-123456" not in err and "sk-other-abcdefgh" not in err and "HTTP 401" in err
