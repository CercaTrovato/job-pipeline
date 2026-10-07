import json
import sqlite3

from jp.duplicate_coverage import audit_clusters, promote_eligible_representatives
from jp.rules.hard import load_constraints


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, source TEXT, region TEXT, company TEXT, title TEXT, "
        "status TEXT, prescore REAL, location TEXT, url TEXT, jd_text TEXT, raw_json TEXT)"
    )
    conn.execute("CREATE TABLE analyses (job_id TEXT PRIMARY KEY, payload TEXT)")
    return conn


def _job(conn, job_id, status="fetched", company="可信科技", title="AI 应用开发实习生", source="watchlist",
         prescore=45, location="深圳", jd="参与 AI 应用开发与数据分析。", raw=None):
    if raw is None:
        raw = {"watchlist": {"ats": "workday", "company": company}}
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, 'CN', ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, source, company, title, status, prescore, location,
         "https://careers.example.com/jobs/" + job_id, jd, json.dumps(raw, ensure_ascii=False)),
    )


def _child(conn, job_id, representative_id):
    _job(conn, job_id, status="duplicate", raw={"dup_of": representative_id})


def _constraints():
    return load_constraints("tests/fixtures/constraints.yaml")


def test_low_scoring_product_manager_cluster_is_audited_but_not_promoted():
    conn = _conn()
    _job(conn, "product", title="产品经理", prescore=14)
    for i in range(14):
        _child(conn, "product-child-%02d" % i, "product")
    audited = audit_clusters(conn, _constraints())
    assert len(audited) == 1
    assert audited[0]["children_count"] == 14
    assert not audited[0]["eligible"]
    assert promote_eligible_representatives(conn, _constraints()) == []
    assert conn.execute("SELECT status FROM jobs WHERE job_id='product'").fetchone()[0] == "fetched"


def test_hard_fail_candidate_is_not_promoted_and_reason_names_hard_condition():
    conn = _conn()
    _job(conn, "hard", jd="要求全职工作，必须全职到岗。" * 8)
    _child(conn, "hard-child", "hard")
    result = audit_clusters(conn, _constraints())[0]
    assert not result["eligible"]
    assert "硬条件不符合" in result["reason"]
    assert promote_eligible_representatives(conn, _constraints()) == []


def test_trusted_cn_ai_fetched_representative_is_promoted_once():
    conn = _conn()
    _job(conn, "good")
    _child(conn, "good-child", "good")
    assert promote_eligible_representatives(conn, _constraints(), max_new=3) == ["good"]
    assert conn.execute("SELECT status FROM jobs WHERE job_id='good'").fetchone()[0] == "queued"
    assert conn.execute("SELECT status FROM jobs WHERE job_id='good-child'").fetchone()[0] == "duplicate"


def test_chinese_adjacent_ai_title_is_eligible():
    conn = _conn()
    _job(conn, "ai", title="AI应用开发实习生")
    _child(conn, "ai-child", "ai")
    assert audit_clusters(conn, _constraints())[0]["eligible"]


def test_untrusted_nowcoder_template_is_not_promoted():
    conn = _conn()
    _job(conn, "template", source="nowcoder", raw={})
    _child(conn, "template-child", "template")
    audited = audit_clusters(conn, _constraints())[0]
    assert not audited["eligible"]
    assert "来源" in audited["reason"]
    assert promote_eligible_representatives(conn, _constraints()) == []


def test_existing_same_brand_approved_exact_prevents_promotion():
    conn = _conn()
    jd = "负责 AI 应用开发，完成需求分析、编码测试与上线维护。" * 8
    _job(conn, "approved", status="approved", company="华为技术有限公司", title="AI 应用开发实习生", jd=jd)
    _job(conn, "candidate", company="上海华为软件技术有限公司", jd=jd)
    _child(conn, "candidate-child", "candidate")
    row = audit_clusters(conn, _constraints())[0]
    assert not row["eligible"]
    assert "已选岗位" in row["reason"]
    assert promote_eligible_representatives(conn, _constraints()) == []
