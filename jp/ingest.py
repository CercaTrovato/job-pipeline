from __future__ import annotations
import json
import pathlib
from dataclasses import dataclass, field
from typing import List

from jp import db as jpdb
from jp import normalize as n
from jp.models import RawJob, Status


@dataclass
class IngestResult:
    new: int = 0
    seen: int = 0
    merged: int = 0
    upgraded: int = 0      # 之前只有列表字段（详情被跳过）的岗位这次拿到完整 JD，已补全
    job_ids: List[str] = field(default_factory=list)


def _upgrade_if_list_only(conn, existing, rj: RawJob, now: str) -> bool:
    """库里那条是"详情被跳过、只有列表字段"的行，而这次抓到了完整详情 → 补全 jd_text / location / salary / raw。"""
    try:
        old_raw = json.loads(existing["raw_json"] or "{}")
    except ValueError:
        old_raw = {}
    if not old_raw.get("detail_skipped"):
        return False
    if rj.raw.get("detail_skipped") or len(rj.jd_text or "") <= len(existing["jd_text"] or ""):
        if rj.raw.get("detail_skipped") == "error":            # 又没读到：计一次，known_ids 据此决定是否还重试
            old_raw["detail_attempts"] = int(old_raw.get("detail_attempts") or 0) + 1
            conn.execute("UPDATE jobs SET raw_json=? WHERE job_id=?", (jpdb.json_dumps(old_raw), existing["job_id"]))
        return False
    raw = dict(old_raw)
    raw.update(rj.raw)
    raw.pop("detail_skipped", None)
    conn.execute(
        "UPDATE jobs SET jd_text=?, jd_lang=?, location=?, salary_raw=?, raw_json=?, last_seen_at=? WHERE job_id=?",
        (rj.jd_text, rj.jd_lang or n.detect_lang(rj.jd_text), rj.location or existing["location"],
         rj.salary_raw or existing["salary_raw"], jpdb.json_dumps(raw), now, existing["job_id"]))
    return True


def load_raw_file(path) -> List[RawJob]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = [data]
    return [RawJob.from_dict(d) for d in data]


def ingest_jobs(conn, raw_jobs: List[RawJob], source: str, region: str) -> IngestResult:
    res = IngestResult()
    now = jpdb.now_iso()
    try:
        for rj in raw_jobs:
            fp = n.fingerprint(rj.company, rj.title, rj.location)
            job_id = n.make_job_id(source, rj.platform_id, rj.company, rj.title, rj.location)

            existing = jpdb.get_job(conn, job_id)
            if existing is not None:
                if _upgrade_if_list_only(conn, existing, rj, now):
                    res.upgraded += 1
                else:
                    conn.execute("UPDATE jobs SET last_seen_at=? WHERE job_id=?", (now, job_id))
                    res.seen += 1
                res.job_ids.append(job_id)
                continue

            if rj.platform_id:
                src_row = conn.execute(
                    "SELECT job_id FROM job_sources WHERE source=? AND platform_id=?",
                    (source, rj.platform_id),
                ).fetchone()
                if src_row is not None:
                    conn.execute("UPDATE jobs SET last_seen_at=? WHERE job_id=?", (now, src_row["job_id"]))
                    res.seen += 1
                    res.job_ids.append(src_row["job_id"])
                    continue

            twin = conn.execute(
                "SELECT job_id FROM jobs WHERE fingerprint=? AND region=? AND source != ?",
                (fp, region, source),
            ).fetchone()
            if twin is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO job_sources(job_id, source, platform_id, url) VALUES(?,?,?,?)",
                    (twin["job_id"], source, rj.platform_id or "", rj.url),
                )
                conn.execute("UPDATE jobs SET last_seen_at=? WHERE job_id=?", (now, twin["job_id"]))
                res.merged += 1
                res.job_ids.append(twin["job_id"])
                continue

            status = Status.QUEUED if source == "referral" else Status.FETCHED
            conn.execute(
                "INSERT INTO jobs(job_id, source, region, platform_id, company, title, location, url, jd_text, jd_lang, "
                "salary_raw, posted_at, fetched_at, last_seen_at, raw_json, fingerprint, status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, source, region, rj.platform_id or "", rj.company, rj.title, rj.location, rj.url,
                 rj.jd_text, rj.jd_lang or n.detect_lang(rj.jd_text), rj.salary_raw, rj.posted_at, now, now,
                 jpdb.json_dumps(rj.raw), fp, status),
            )
            conn.execute(
                "INSERT OR IGNORE INTO job_sources(job_id, source, platform_id, url) VALUES(?,?,?,?)",
                (job_id, source, rj.platform_id or "", rj.url),
            )
            res.new += 1
            res.job_ids.append(job_id)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return res
