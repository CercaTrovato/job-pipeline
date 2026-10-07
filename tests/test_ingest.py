from __future__ import annotations
import pathlib
from jp import ingest
from jp.models import RawJob, Status

FX = pathlib.Path(__file__).resolve().parent / "fixtures" / "jd"

def _raw(pid="p1", company="A 公司", title="算法实习生", loc="深圳", url="https://x/1"):
    return RawJob(platform_id=pid, title=title, company=company, url=url, jd_text="jd", location=loc)

def test_ingest_new_then_seen(conn):
    r1 = ingest.ingest_jobs(conn, [_raw()], source="boss", region="CN")
    assert (r1.new, r1.seen, r1.merged) == (1, 0, 0)
    r2 = ingest.ingest_jobs(conn, [_raw()], source="boss", region="CN")
    assert (r2.new, r2.seen, r2.merged) == (0, 1, 0)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1

def test_list_only_row_is_upgraded_when_full_detail_arrives(conn):
    """上次只带列表字段入库（详情被预筛跳过）的岗位，再次抓到完整 JD 时补全 jd_text / raw，不算新增。"""
    import json
    thin = RawJob(platform_id="p1", title="算法实习生", company="A 公司", url="https://x/1", jd_text="到岗要求：5天/周 3个月",
                  location="深圳", raw={"days_per_week": 5, "detail_skipped": "hard_prefilter", "list": {"a": 1}})
    ingest.ingest_jobs(conn, [thin], source="boss", region="CN")
    full = RawJob(platform_id="p1", title="算法实习生", company="A 公司", url="https://x/1", jd_text="岗位职责：训练大模型……\n到岗要求：5天/周 3个月",
                  location="深圳·南山区", salary_raw="300元/天", raw={"days_per_week": 5, "detail": {"x": 1}, "list": {"a": 1}})
    r = ingest.ingest_jobs(conn, [full], source="boss", region="CN")
    assert (r.new, r.seen, r.upgraded) == (0, 0, 1)
    row = conn.execute("SELECT jd_text, location, salary_raw, raw_json, status FROM jobs").fetchone()
    assert row["jd_text"].startswith("岗位职责") and row["location"] == "深圳·南山区" and row["salary_raw"] == "300元/天"
    raw = json.loads(row["raw_json"])
    assert "detail_skipped" not in raw and raw["detail"] == {"x": 1} and row["status"] == Status.FETCHED
    # 再来一次列表行（known 跳过）不会把完整 JD 又覆盖回去
    again = RawJob(platform_id="p1", title="算法实习生", company="A 公司", url="https://x/1", jd_text="到岗要求：5天/周 3个月",
                   location="深圳", raw={"detail_skipped": "known"})
    r2 = ingest.ingest_jobs(conn, [again], source="boss", region="CN")
    assert (r2.seen, r2.upgraded) == (1, 0)
    assert conn.execute("SELECT jd_text FROM jobs").fetchone()[0].startswith("岗位职责")


def test_repeated_detail_error_counts_attempts(conn):
    import json
    thin = RawJob(platform_id="p9", title="算法实习生", company="A 公司", url="https://x/9", jd_text="到岗要求：4天/周",
                  location="深圳", raw={"detail_skipped": "error"})
    ingest.ingest_jobs(conn, [thin], source="boss", region="CN")
    ingest.ingest_jobs(conn, [thin], source="boss", region="CN")
    r = ingest.ingest_jobs(conn, [thin], source="boss", region="CN")
    assert r.seen == 1
    assert json.loads(conn.execute("SELECT raw_json FROM jobs").fetchone()[0])["detail_attempts"] == 2


def test_cross_platform_merge_into_job_sources(conn):
    ingest.ingest_jobs(conn, [_raw(pid="b1")], source="boss", region="CN")
    r = ingest.ingest_jobs(conn, [_raw(pid="n1", url="https://n/1")], source="nowcoder", region="CN")
    assert (r.new, r.merged) == (0, 1)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    srcs = {row["source"] for row in conn.execute("SELECT source FROM job_sources")}
    assert srcs == {"boss", "nowcoder"}

def test_referral_goes_straight_to_queued(conn):
    raws = ingest.load_raw_file(FX / "referral_cn.json")
    r = ingest.ingest_jobs(conn, raws, source="referral", region="CN")
    row = conn.execute("SELECT status, jd_lang, fingerprint FROM jobs WHERE job_id=?", (r.job_ids[0],)).fetchone()
    assert row["status"] == Status.QUEUED and row["jd_lang"] == "zh" and row["fingerprint"]

def test_missing_required_field_raises():
    import pytest
    with pytest.raises(ValueError):
        RawJob.from_dict({"title": "x", "company": "y"})

def test_same_source_same_fingerprint_not_merged(conn):
    ingest.ingest_jobs(conn, [_raw(pid="b1")], source="boss", region="CN")
    r2 = ingest.ingest_jobs(conn, [_raw(pid="b2")], source="boss", region="CN")
    assert r2.new == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2

def test_merged_source_rerun_counts_seen(conn):
    ingest.ingest_jobs(conn, [_raw(pid="b1")], source="boss", region="CN")
    r_merge = ingest.ingest_jobs(conn, [_raw(pid="n1", url="https://n/1")], source="nowcoder", region="CN")
    assert r_merge.merged == 1
    r_rerun = ingest.ingest_jobs(conn, [_raw(pid="n1", url="https://n/1")], source="nowcoder", region="CN")
    assert (r_rerun.new, r_rerun.seen, r_rerun.merged) == (0, 1, 0)

def test_failed_batch_rolls_back(conn):
    import pytest
    import sqlite3
    bad = RawJob(platform_id="bad1", title="t", company="c", url="https://x/2", jd_text=None, location="深圳")
    with pytest.raises(sqlite3.IntegrityError):
        ingest.ingest_jobs(conn, [_raw(pid="ok1"), bad], source="boss", region="CN")
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
