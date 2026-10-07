from __future__ import annotations
import hashlib
import json
import pathlib
import pytest
from board.app import create_app
from jp import db as jpdb, ingest
from jp.models import RawJob, Status

ROOT = pathlib.Path(__file__).resolve().parents[1]

@pytest.fixture
def client(tmp_path):
    db = tmp_path / "b.sqlite"
    c = jpdb.connect(db); jpdb.init_db(c, ROOT / "db" / "schema.sql")
    r = ingest.ingest_jobs(c, [RawJob(platform_id="p1", title="LLM 实习", company="智谱", url="https://x/1", jd_text="jd", location="深圳")], "boss", "CN")
    jid = r.job_ids[0]
    jpdb.set_status(c, jid, Status.PENDING_REVIEW)
    a = {"summary": "深圳 LLM 实习", "atomic_requirements": [{"id": "R1", "quote": "q", "requirement": "Python", "level": "high", "kind": "skill"}],
         "keywords": {"must": ["Python"], "core": [], "bonus": []}, "hard_conditions": {}, "flags": {}}
    m = {"items": [{"req_id": "R1", "evidence_grade": "A", "fact_ids": ["x"], "verdict": "strong", "gap_type": None, "note": ""}], "rationale": "值得投"}
    c.execute("INSERT INTO analyses VALUES(?,?,?,?,?)", (jid, "h", json.dumps(a, ensure_ascii=False), "packet", jpdb.now_iso()))
    c.execute("INSERT INTO matches VALUES(?,?,?,?,?,?,?,?)", (jid, "h", 1, "[]", json.dumps(m, ensure_ascii=False), 88.0, 1, jpdb.now_iso()))
    c.execute("INSERT INTO runs(command, source, started_at, status, pages_done, pages_total) VALUES('fetch','boss',?, 'done', 3, 3)", (jpdb.now_iso(),))
    c.commit(); c.close()
    app = create_app(db, ROOT / "db" / "schema.sql")
    app.config["TESTING"] = True
    yield app.test_client(), jid

def test_index_lists_pending(client):
    cl, jid = client
    html = cl.get("/").get_data(as_text=True)
    assert "智谱" in html and "88.0" in html and jid in html

def test_board_serves_its_own_icon(client):
    cl, _ = client
    assert 'href="/favicon.ico"' in cl.get("/").get_data(as_text=True)
    icon = cl.get("/favicon.ico")
    assert icon.status_code == 200
    assert icon.data[:4] == b"\x00\x00\x01\x00"


def test_duplicate_shows_representative_and_reason(client):
    cl, representative_id = client
    conn = jpdb.connect(cl.application.config["DB_PATH"])
    jpdb.set_status(conn, representative_id, Status.APPROVED)
    child_id = ingest.ingest_jobs(conn, [RawJob(platform_id="copy", title="LLM 实习",
                                               company="另一家公司", url="https://x/copy",
                                               jd_text="jd", location="深圳")], "nowcoder", "CN").job_ids[0]
    jpdb.set_status(conn, child_id, Status.DUPLICATE)
    conn.execute("UPDATE jobs SET raw_json=? WHERE job_id=?",
                 (json.dumps({"dup_of": representative_id,
                              "dup_reason": "疑似刷帖：同一 JD 挂在 5 家公司名下"}, ensure_ascii=False), child_id))
    conn.commit(); conn.close()

    listing = cl.get("/prescreened?status=duplicate").get_data(as_text=True)
    detail = cl.get("/job/%s" % child_id).get_data(as_text=True)
    assert "疑似刷帖：同一 JD 挂在 5 家公司名下" in listing
    assert "智谱 · LLM 实习" in listing and representative_id in listing
    assert "已选待投递" in listing
    assert "智谱 · LLM 实习" in detail and "公司归属须以招聘官网" in detail
    assert '粗筛淘汰 <span class="count">1</span>' in listing


def test_job_detail_and_decide(client):
    cl, jid = client
    assert "值得投" in cl.get("/job/%s" % jid).get_data(as_text=True)
    resp = cl.post("/decide/%s" % jid, data={"decision": "apply", "note": "go"}, follow_redirects=True)
    assert resp.status_code == 200 and jid not in resp.get_data(as_text=True)
    selected = cl.get("/selected").get_data(as_text=True)
    assert jid in selected and "已选待投递" in selected and "尚未执行" in selected


def test_job_detail_marks_nonliteral_model_quote(client):
    cl, jid = client
    conn = jpdb.connect(cl.application.config["DB_PATH"])
    row = conn.execute("SELECT payload FROM analyses WHERE job_id=?", (jid,)).fetchone()
    analysis = json.loads(row["payload"])
    analysis["atomic_requirements"][0]["quote"] = "不存在的模型引文"
    conn.execute("UPDATE analyses SET payload=? WHERE job_id=?", (json.dumps(analysis, ensure_ascii=False), jid))
    conn.commit(); conn.close()
    html = cl.get("/job/%s" % jid).get_data(as_text=True)
    assert "模型引文与 JD 原文不一致" in html and "R1" in html

def test_referral_ingest(client):
    cl, _ = client
    url = "https://ref/1"
    resp = cl.post("/referral", data={"jd_text": "招聘数据分析实习生，熟悉 SQL", "url": url, "company": "", "title": "", "region": "HK"},
                   follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert "已入库" in html and "运行页面启动分析" in html
    app = cl.application
    c = jpdb.connect(app.config["DB_PATH"])
    platform_id = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    row = c.execute("SELECT * FROM jobs WHERE platform_id=?", (platform_id,)).fetchone()
    c.close()
    assert row is not None
    assert row["source"] == "referral"
    assert row["company"] == "(待抽取)"
    assert row["title"] == "(待抽取)"
    assert row["platform_id"] == platform_id
    assert row["status"] == "queued"
    assert row["region"] == "HK"

def test_runs_json(client):
    cl, _ = client
    data = cl.get("/runs.json").get_json()
    assert data[0]["command"] == "fetch" and data[0]["pages_done"] == 3

def test_prescreened_requeue(client, tmp_path):
    cl, _ = client
    app = cl.application
    c = jpdb.connect(app.config["DB_PATH"])
    r = ingest.ingest_jobs(c, [RawJob(platform_id="p9", title="X", company="Y", url="u", jd_text="jd")], "boss", "CN")
    jpdb.set_status(c, r.job_ids[0], Status.PRESCREENED_OUT); c.close()
    assert "Y" in cl.get("/prescreened").get_data(as_text=True)
    cl.post("/requeue/%s" % r.job_ids[0], follow_redirects=True)
    c = jpdb.connect(app.config["DB_PATH"])
    assert c.execute("SELECT status FROM jobs WHERE job_id=?", (r.job_ids[0],)).fetchone()[0] == Status.QUEUED


def test_runs_page_polls_without_innerhtml_concat(client):
    cl, _ = client
    resp = cl.get("/runs")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "/runs.json" in html
    assert "innerHTML" not in html.replace(" ", "")


def test_job_detail_with_orphan_req_id(client):
    cl, _ = client
    app = cl.application
    c = jpdb.connect(app.config["DB_PATH"])
    r = ingest.ingest_jobs(c, [RawJob(platform_id="p2", title="数据分析实习", company="乙公司", url="https://x/2", jd_text="jd2", location="上海")], "boss", "CN")
    jid2 = r.job_ids[0]
    jpdb.set_status(c, jid2, Status.PENDING_REVIEW)
    a = {"summary": "上海数据分析实习", "atomic_requirements": [{"id": "R1", "quote": "q", "requirement": "SQL", "level": "high", "kind": "skill"}],
         "keywords": {"must": ["SQL"], "core": [], "bonus": []}, "hard_conditions": {}, "flags": {}}
    m = {"items": [{"req_id": "R9", "evidence_grade": "B", "fact_ids": [], "verdict": "weak", "gap_type": "missing", "note": "孤儿需求"}], "rationale": "待定"}
    c.execute("INSERT INTO analyses VALUES(?,?,?,?,?)", (jid2, "h2", json.dumps(a, ensure_ascii=False), "packet", jpdb.now_iso()))
    c.execute("INSERT INTO matches VALUES(?,?,?,?,?,?,?,?)", (jid2, "h2", 1, "[]", json.dumps(m, ensure_ascii=False), 50.0, 1, jpdb.now_iso()))
    c.commit(); c.close()
    resp = cl.get("/job/%s" % jid2)
    assert resp.status_code == 200
    assert "R9" in resp.get_data(as_text=True)


def test_two_bare_referrals_do_not_collide(client):
    cl, _ = client
    for jd in ("招聘 A 岗位，熟悉 Python", "招聘 B 岗位，熟悉 SQL"):
        cl.post("/referral", data={"jd_text": jd, "url": "", "company": "", "title": "", "region": "CN"}, follow_redirects=True)
    c = jpdb.connect(cl.application.config["DB_PATH"])
    rows = c.execute("SELECT jd_text FROM jobs WHERE source='referral' ORDER BY jd_text").fetchall()
    assert [r["jd_text"] for r in rows] == ["招聘 A 岗位，熟悉 Python", "招聘 B 岗位，熟悉 SQL"]
