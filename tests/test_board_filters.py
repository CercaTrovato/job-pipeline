from __future__ import annotations
import json
import pathlib
from urllib.parse import parse_qs, urlsplit

import pytest
from werkzeug.datastructures import MultiDict
from board.app import create_app
from jp import browse, db, ingest
from jp.models import RawJob, Status

ROOT = pathlib.Path(__file__).resolve().parents[1]


def seed(conn, company, title, region="CN", status=Status.PRESCREENED_OUT, jd="RAG Python"):
    jid = ingest.ingest_jobs(conn, [RawJob(platform_id=company + title, title=title, company=company,
                                         url="https://example.org/job", jd_text=jd, location="深圳")],
                             "watchlist", region).job_ids[0]
    db.set_status(conn, jid, status)
    if status == Status.PENDING_REVIEW:
        a = {"summary": "岗位摘要", "atomic_requirements": []}
        m = {"items": [], "rationale": "测试"}
        conn.execute("INSERT INTO analyses VALUES(?,?,?,?,?)", (jid, "h", json.dumps(a), "packet", db.now_iso()))
        conn.execute("INSERT INTO matches VALUES(?,?,?,?,?,?,?,?)",
                     (jid, "h", 1, "[]", json.dumps(m), 80, 1, db.now_iso()))
        conn.commit()
    return jid


@pytest.mark.parametrize("title,required,excluded", [
    ("大模型招聘负责人", {"corporate"}, {"ai"}),
    ("AI Agent 产品运营实习生", {"product", "ops"}, {"ai", "software"}),
    ("Data Scientist Intern", {"ai", "data"}, set()),
    ("AI应用后端开发实习生", {"backend"}, {"software"}),
    ("Software Engineer", {"software"}, {"frontend"}),
    ("HR Training Partner", {"corporate"}, {"ai"}),
    ("未知职位", {"other"}, set()),
])
def test_title_classification(title, required, excluded):
    roles, _ = browse.title_tags(title)
    assert required <= set(roles) and not excluded.intersection(roles)


def test_stage_is_title_evidence_only():
    assert browse.title_tags("2027 校招实习生")[1] == ["intern", "campus"]
    assert browse.title_tags("Senior Engineer")[1] == ["unspecified"]


def test_rule_track_overrides_title_stage(conn):
    """规则判定的轨道（raw_json.track，来自 prescore / recheck）优先于标题字样：校招帖标题没有"校招"也归到校招。"""
    import json
    a = seed(conn, "华为", "算法工程师", status=Status.FETCHED)
    b = seed(conn, "腾讯", "算法实习生", status=Status.FETCHED)
    conn.execute("UPDATE jobs SET raw_json=? WHERE job_id=?", (json.dumps({"track": "campus", "graduation_year": "毕业不限"}), a))
    conn.commit()
    rows = {r["job_id"]: r for r in browse.list_jobs(conn, "prescreened", MultiDict({}))["rows"]}
    assert rows[a]["stages"] == ["campus"] and rows[b]["stages"] == ["intern"]
    assert [r["job_id"] for r in browse.list_jobs(conn, "prescreened", MultiDict({"stage": "campus"}))["rows"]] == [a]
    p = seed(conn, "美团", "AI数据开发工程师", status=Status.PENDING_REVIEW)
    conn.execute("UPDATE jobs SET raw_json=? WHERE job_id=?", (json.dumps({"track": "campus"}), p))
    conn.commit()
    assert browse.list_jobs(conn, "pending", MultiDict({}))["rows"][0]["stages"] == ["campus"]


def test_search_combination_and_facets(conn):
    a = seed(conn, "甲公司", "数据分析实习生")
    b = seed(conn, "乙公司", "后端开发", "HK", Status.REJECTED_HARD)
    seed(conn, "甲公司", "行政助理", jd="Excel")
    result = browse.list_jobs(conn, "prescreened", MultiDict([("q", "python RAG"), ("role", "data"), ("role", "backend")]))
    assert {r["job_id"] for r in result["rows"]} == {a, b}
    result = browse.list_jobs(conn, "prescreened", MultiDict({"q": "pYtHoN", "company": "甲", "region": "CN", "role": "data"}))
    assert [r["job_id"] for r in result["rows"]] == [a]
    assert result["total"] == 3 and result["matched"] == 1
    assert browse.list_jobs(conn, "prescreened", MultiDict({"status": "rejected_hard"}))["matched"] == 1
    assert browse.list_jobs(conn, "prescreened", MultiDict({"q": "% OR 1=1"}))["matched"] == 0


def test_backlog_fetched_jobs_show_in_prescreened_page_and_can_requeue(conn):
    """prescore 之后留在 fetched 的候补（相关但没排进 top_n）也在粗筛页可见、可筛、可重新入队。"""
    from jp import decide
    a = seed(conn, "甲公司", "数据分析实习生", status=Status.FETCHED)
    b = seed(conn, "乙公司", "行政助理", status=Status.PRESCREENED_OUT)
    result = browse.list_jobs(conn, "prescreened", MultiDict({}))
    assert {r["job_id"] for r in result["rows"]} == {a, b} and result["total"] == 2
    assert [r["job_id"] for r in browse.list_jobs(conn, "prescreened", MultiDict({"status": "fetched"}))["rows"]] == [a]
    assert dict((v, n) for v, n, _ in result["facets"]["status"])["fetched"] == "候补（未分析）"
    decide.requeue(conn, a)
    assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (a,)).fetchone()[0] == Status.QUEUED


def test_duplicate_jobs_show_in_prescreened_page_and_can_requeue(conn):
    """折叠掉的重复帖 / 刷帖在粗筛页可见、可筛、可重新入队（入队后不会再被 dedup 折叠回去）。"""
    import json
    from jp import decide
    a = seed(conn, "甲公司", "大模型算法实习生", status=Status.DUPLICATE)
    conn.execute("UPDATE jobs SET raw_json=? WHERE job_id=?",
                 (json.dumps({"dup_of": "x", "dup_reason": "疑似刷帖：同一 JD 挂在 4 家公司名下"}), a))
    conn.commit()
    result = browse.list_jobs(conn, "prescreened", MultiDict({"status": "duplicate"}))
    assert [r["job_id"] for r in result["rows"]] == [a]
    assert dict((v, n) for v, n, _ in result["facets"]["status"])["duplicate"] == "重复帖 / 疑似刷帖"
    decide.requeue(conn, a)
    assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (a,)).fetchone()[0] == Status.QUEUED
    assert json.loads(conn.execute("SELECT raw_json FROM jobs WHERE job_id=?", (a,)).fetchone()[0])["dup_keep"] is True


def test_pending_search_includes_jd_location(conn):
    seed(conn, "甲", "算法实习", status=Status.PENDING_REVIEW, jd="独特全文词")
    seed(conn, "乙", "算法实习", status=Status.PENDING_REVIEW, jd="其它")
    result = browse.list_jobs(conn, "pending", MultiDict({"q": "深圳 独特全文词"}))
    assert result["matched"] == 1


def test_pagination_and_selected_zero_count(conn):
    for n in range(33):
        seed(conn, "甲", "后端实习 %02d" % n)
    result = browse.list_jobs(conn, "prescreened", MultiDict({"company": "甲", "page": "2"}))
    assert len(result["rows"]) == 3 and result["pages"] == 2
    assert parse_qs(urlsplit(result["previous"]).query)["company"] == ["甲"]
    assert browse.list_jobs(conn, "prescreened", MultiDict({"page": "bad"}))["page_number"] == 1
    assert browse.list_jobs(conn, "prescreened", MultiDict({"page": "999"}))["page_number"] == 2
    empty = browse.list_jobs(conn, "prescreened", MultiDict({"role": "data"}))
    assert empty["matched"] == 0 and ("data", "数据分析", 0) in empty["role_options"]


@pytest.mark.parametrize("url", ["https://evil.test/", "//evil.test/", "/referral", "/\\evil.test/", "http://["])
def test_return_rejects_external_or_unrelated_paths(url):
    assert browse.safe_return(url) == "/"


def test_list_detail_and_requeue_keep_filters(tmp_path):
    path = tmp_path / "board.sqlite"
    conn = db.connect(path)
    db.init_db(conn, ROOT / "db/schema.sql")
    jid = seed(conn, "甲公司", "后端实习")
    seed(conn, "乙公司", "数据分析实习")
    conn.close()
    cl = create_app(path, ROOT / "db/schema.sql").test_client()
    query = "/prescreened?company=" + "%E7%94%B2" + "&role=backend"
    response = cl.get(query)
    body = response.get_data(as_text=True)
    assert 'name="q"' in body and 'name="role"' in body and "后端开发" in body
    assert "/job/" + jid in body and 'name="return_to"' in body
    detail = cl.get("/job/" + jid, query_string={"return_to": query}).get_data(as_text=True)
    assert "返回粗筛淘汰" in detail and "role=backend" in detail
    response = cl.post("/requeue/" + jid, data={"return_to": query})
    assert parse_qs(urlsplit(response.location).query)["role"] == ["backend"]
    empty = cl.get(query).get_data(as_text=True)
    assert "没有符合条件的岗位" in empty and "清除全部" in empty


def test_pending_action_and_search_escaping(tmp_path):
    path = tmp_path / "board.sqlite"
    conn = db.connect(path)
    db.init_db(conn, ROOT / "db/schema.sql")
    jid = seed(conn, "甲", "AI工程师", status=Status.PENDING_REVIEW)
    conn.close()
    cl = create_app(path, ROOT / "db/schema.sql").test_client()
    html = cl.get("/", query_string={"q": '<script>alert(1)</script>'}).get_data(as_text=True)
    assert '<script>alert(1)</script>' not in html and "&lt;script&gt;" in html
    response = cl.post("/decide/" + jid, data={"decision": "later", "return_to": "/?company=甲"})
    assert parse_qs(urlsplit(response.location).query)["company"] == ["甲"]
