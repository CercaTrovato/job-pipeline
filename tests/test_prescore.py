from __future__ import annotations
import json
import pathlib
from jp import prescore, ingest
from jp.models import RawJob, Status
from jp.rules import hard
from jp import facts_index as fi

ROOT = pathlib.Path(__file__).resolve().parents[1]
FX = ROOT / "tests" / "fixtures" / "facts"
C = hard.load_constraints(ROOT / "tests" / "fixtures" / "constraints.yaml")

def _kw():
    return {k for e in fi.build(FX) for k in e["keywords"]}

def test_score_text_overlap():
    s, hits = prescore.score_text("需要 Python、RAG 与 PyTorch 经验", "", _kw())
    assert s > 0 and {"python", "rag", "pytorch"} <= set(hits)
    s0, hits0 = prescore.score_text("需要 焊接 与 叉车 经验", "", _kw())
    assert s0 == 0 and hits0 == []

def _add(conn, pid, jd, region="CN"):
    ingest.ingest_jobs(conn, [RawJob(platform_id=pid, title="t" + pid, company="c" + pid, url="u", jd_text=jd, location="深圳")],
                       source="boss", region=region)

def test_run_transitions(conn):
    _add(conn, "1", "Python RAG PyTorch 实习生，每周 4 天")
    _add(conn, "2", "焊接叉车")
    _add(conn, "3", "Python 实习生，实习 8 个月以上")
    _add(conn, "4", "Python 实习生，每周 5 天到岗")
    entries = fi.build(FX)
    r = prescore.run(conn, entries, C, top_n=10, min_overlap=1)
    assert (r.queued, r.out, r.killed) == (2, 1, 1)
    rows = {row["platform_id"]: row for row in conn.execute("SELECT platform_id, status, prescore, raw_json FROM jobs")}
    assert rows["1"]["status"] == Status.QUEUED and rows["1"]["prescore"] > 0
    assert rows["2"]["status"] == Status.PRESCREENED_OUT
    assert rows["3"]["status"] == Status.REJECTED_HARD
    assert json.loads(rows["3"]["raw_json"])["prescreen_reasons"] == ["要求 ≥7 个月"]
    assert rows["4"]["status"] == Status.QUEUED and json.loads(rows["4"]["raw_json"])["soft_flags"] == ["要求每周 5 天（可谈）"]

def test_top_n_cutoff_keeps_backlog_as_fetched(conn):
    """top_n 之外但相关的岗位不淘汰，留在 fetched 当候补；下一次 prescore 继续按分数出队。"""
    for i in range(5):
        _add(conn, str(i), "Python 实习")
    r = prescore.run(conn, fi.build(FX), C, top_n=2, min_overlap=1)
    assert (r.queued, r.out, r.backlog) == (2, 0, 3)
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE status='fetched'").fetchone()[0] == 3
    r2 = prescore.run(conn, fi.build(FX), C, top_n=2, min_overlap=1)
    assert (r2.queued, r2.backlog) == (0, 3)                   # 队列还满着：不再出队
    from jp import db as jpdb
    for row in conn.execute("SELECT job_id FROM jobs WHERE status='queued'").fetchall():
        jpdb.set_status(conn, row[0], Status.SKIPPED)
    r3 = prescore.run(conn, fi.build(FX), C, top_n=2, min_overlap=1)
    assert (r3.queued, r3.backlog) == (2, 1)                   # 队列空出来后继续按分数出队


def test_score_text_idf_weighted_distinct_hits_and_title_bonus():
    kw = {"python", "pytorch", "rag", "数据", "项目", "开发"}
    idf = {"python": 2.0, "pytorch": 5.0, "rag": 5.0, "数据": 1.0, "项目": 1.0, "开发": 1.0}
    generic, _ = prescore.score_text("负责数据项目开发，数据数据数据项目项目开发开发", "", kw, idf)
    specific, hits = prescore.score_text("用 PyTorch 与 RAG 做 Python 开发", "", kw, idf)
    assert specific > generic and {"pytorch", "rag", "python"} <= set(hits)
    titled, _ = prescore.score_text("用 PyTorch 与 RAG 做 Python 开发", "RAG 实习生", kw, idf)
    assert titled > specific                                   # 标题命中再加分
    assert prescore.score_text("焊接叉车", "", kw, idf) == (0.0, [])


def test_role_prior_demotes_non_tech_titles():
    kw = {"python", "数据", "分析"}
    idf = {"python": 2.0, "数据": 1.5, "分析": 1.5}
    tech, _ = prescore.score_text("Python 数据分析", "数据分析实习生", kw, idf)
    product, _ = prescore.score_text("Python 数据分析", "产品经理实习生", kw, idf)
    hr, _ = prescore.score_text("Python 数据分析", "HR实习生", kw, idf)
    assert tech > product and tech > hr


def test_run_ranks_specific_over_generic_and_records_hits(conn):
    _add(conn, "g", "负责数据项目开发，撰写报告，参与系统测试")
    _add(conn, "s", "用 PyTorch 做聚类与图学习，接触 RAG 与 Agent")
    r = prescore.run(conn, fi.build(FX), C, top_n=1, min_overlap=1)
    assert (r.queued, r.backlog) == (1, 1)
    rows = {row["platform_id"]: row for row in conn.execute("SELECT platform_id, status, prescore, raw_json FROM jobs")}
    assert rows["s"]["status"] == Status.QUEUED and rows["g"]["status"] == Status.FETCHED
    assert rows["s"]["prescore"] > rows["g"]["prescore"]
    assert "pytorch" in json.loads(rows["s"]["raw_json"])["prescore_hits"]

def test_interleave_by_region_alternates_then_fills():
    scored = [(90.0, "c1", "CN"), (80.0, "c2", "CN"), (70.0, "c3", "CN"), (60.0, "h1", "HK"), (50.0, "c4", "CN")]
    assert [j for _, j, _ in prescore.interleave_by_region(scored)] == ["c1", "h1", "c2", "c3", "c4"]
    assert prescore.interleave_by_region([]) == []


def test_run_queues_regions_round_robin(conn):
    for i in range(3):
        _add(conn, "cn%d" % i, "Python PyTorch RAG 实习", region="CN")
    ingest.ingest_jobs(conn, [RawJob(platform_id="hk", title="Data Intern", company="H", url="u", jd_text="python", location="Hong Kong")],
                       source="jobsdb", region="HK")
    r = prescore.run(conn, fi.build(FX), C, top_n=2, min_overlap=1)
    assert (r.queued, r.backlog) == (2, 2)
    q = {row["region"] for row in conn.execute("SELECT region FROM jobs WHERE status='queued'")}
    assert q == {"CN", "HK"}                                   # 分低的 HK 岗位也占到一个名额


def test_top_n_is_queue_capacity_not_increment(conn):
    """run 先粗筛再出队：已有 queued 时只补齐到 top_n，不额外再出 top_n 条。"""
    for i in range(5):
        _add(conn, str(i), "Python 实习")
    prescore.run(conn, fi.build(FX), C, top_n=3, min_overlap=1)
    _add(conn, "new", "Python RAG 实习")
    r = prescore.run(conn, fi.build(FX), C, top_n=3, min_overlap=1)
    assert r.queued == 0 and conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0] == 3
    from jp import db as jpdb
    jpdb.set_status(conn, conn.execute("SELECT job_id FROM jobs WHERE status='queued' LIMIT 1").fetchone()[0], Status.SKIPPED)
    r2 = prescore.run(conn, fi.build(FX), C, top_n=3, min_overlap=1)
    assert r2.queued == 1 and conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0] == 3


def test_rerun_is_idempotent(conn):
    _add(conn, "1", "Python 实习")
    prescore.run(conn, fi.build(FX), C, top_n=10, min_overlap=1)
    r = prescore.run(conn, fi.build(FX), C, top_n=10, min_overlap=1)
    assert (r.queued, r.out, r.killed) == (0, 0, 0)

def test_adapter_fields_reject_before_model(conn):
    """抓取器给的地点 / 招聘类型 / 到岗天数在花接口调用前就判硬条件；校招轨道按校招口径。"""
    ingest.ingest_jobs(conn, [
        RawJob(platform_id="bj", title="MaaS 平台开发工程师", company="M", url="u", jd_text="Python PyTorch 模型服务化",
               location="北京，上海", raw={"recruit_type": "全职"}),
        RawJob(platform_id="nc5", title="算法实习生", company="N", url="u", jd_text="Python PyTorch 实习",
               location="深圳", raw={"days_per_week": 5, "min_months": 12}),
        RawJob(platform_id="cd", title="算法工程师（2027届校招）", company="C", url="u", jd_text="Python PyTorch 应届生",
               location="成都", raw={"recruit_type": "全职"}),
        RawJob(platform_id="26", title="算法工程师（26届校招）", company="C", url="u", jd_text="Python PyTorch 应届生",
               location="深圳", raw={"recruit_type": "全职"}),
    ], source="watchlist", region="CN")
    r = prescore.run(conn, fi.build(FX), C, top_n=10, min_overlap=1)
    assert (r.queued, r.out, r.killed) == (1, 0, 3)
    rows = {row["platform_id"]: row for row in conn.execute("SELECT platform_id, status, raw_json FROM jobs")}
    assert rows["cd"]["status"] == Status.QUEUED and json.loads(rows["cd"]["raw_json"])["track"] == "campus"
    assert "地点不符: 北京/上海" in json.loads(rows["bj"]["raw_json"])["prescreen_reasons"]
    assert "雇佣类型不符: fulltime" in json.loads(rows["bj"]["raw_json"])["prescreen_reasons"]
    assert json.loads(rows["nc5"]["raw_json"])["prescreen_reasons"] == ["要求 ≥12 个月"]
    assert json.loads(rows["26"]["raw_json"])["prescreen_reasons"] == ["届别不符: 2026（要求 2027 届）"]
