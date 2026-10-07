from __future__ import annotations

import json
import pathlib

import pytest

from board.app import create_app
from jp import db, decide, ingest
from jp.models import RawJob, Status


ROOT = pathlib.Path(__file__).resolve().parents[1]
JD = "参与 AI Agent 应用开发，使用 Python、RAG 和工具调用构建可靠系统。" * 12


def _seed(conn, pid, company, title, status, source="nowcoder"):
    job_id = ingest.ingest_jobs(conn, [RawJob(platform_id=pid, title=title, company=company,
                                              url="https://example.org/jobs/" + pid,
                                              jd_text=JD, location="深圳")], source, "CN").job_ids[0]
    db.set_status(conn, job_id, status)
    return job_id


def test_cross_brand_requeue_remains_separate_and_visible(conn):
    selected = _seed(conn, "bili", "哔哩哔哩", "AI 应用开发实习", Status.APPROVED)
    child = _seed(conn, "pdd", "拼多多集团-PDD", "AI 应用开发", Status.DUPLICATE)
    conn.execute("UPDATE jobs SET raw_json=? WHERE job_id=?",
                 (json.dumps({"dup_of": selected, "dup_reason": "跨公司同文 JD"}, ensure_ascii=False), child))
    conn.commit()
    decide.requeue(conn, child)
    ordinary = _seed(conn, "ordinary", "另一家公司", "普通算法岗位", Status.QUEUED)
    conn.execute("UPDATE jobs SET prescore=99 WHERE job_id=?", (ordinary,))
    conn.commit()
    row = db.get_job(conn, child)
    raw = json.loads(row["raw_json"])
    assert row["status"] == Status.QUEUED and raw["dup_keep"] is True
    assert raw["requeued_from_dup_of"] == selected and raw["manual_requeued_at"]
    queue = decide.queue_rows(conn)
    assert queue[0]["job_id"] == child and queue[0]["queue_rank"] == 1
    assert queue[0]["same_jd_approved"][0]["job_id"] == selected
    assert conn.execute("SELECT COUNT(*) FROM analyses WHERE job_id=?", (child,)).fetchone()[0] == 0
    another_manual = _seed(conn, "manual-2", "小红书", "另一个 AI 应用岗位", Status.DUPLICATE)
    decide.requeue(conn, another_manual)
    for manual_id in (child, another_manual):
        manual_raw = json.loads(db.get_job(conn, manual_id)["raw_json"])
        manual_raw["manual_requeued_at"] = "2026-01-01T00:00:00"
        conn.execute("UPDATE jobs SET raw_json=? WHERE job_id=?",
                     (json.dumps(manual_raw, ensure_ascii=False), manual_id))
    conn.execute("UPDATE jobs SET prescore=99 WHERE job_id=?", (another_manual,))
    conn.commit()
    assert decide.queue_rows(conn)[0]["job_id"] == another_manual
    decide.set_manual_priority(conn, child, 0)
    assert decide.queue_rows(conn)[0]["job_id"] == child


def test_same_brand_exact_approved_blocks_requeue(conn):
    selected = _seed(conn, "old", "拼多多集团-PDD", "AI 应用开发实习", Status.APPROVED)
    child = _seed(conn, "new", "拼多多", "AI 应用开发", Status.DUPLICATE)
    with pytest.raises(decide.DuplicateConfirmationRequired) as error:
        decide.requeue(conn, child)
    assert error.value.conflicts[0]["related_job_id"] == selected
    assert error.value.allow_override is False
    assert db.get_job(conn, child)["status"] == Status.DUPLICATE


def test_legacy_queued_same_brand_exact_is_visible_but_not_processable(conn):
    selected = _seed(conn, "old", "快手", "多模态研究员", Status.APPROVED)
    queued = _seed(conn, "queued", "北京快手科技有限公司", "Agent 研究员", Status.QUEUED)
    row = next(item for item in decide.queue_rows(conn) if item["job_id"] == queued)
    assert row["blocking_approved"][0]["related_job_id"] == selected


def test_distinct_official_ids_can_be_confirmed_for_requeue(conn):
    selected = _seed(conn, "official-old", "美团", "AI 工程师", Status.APPROVED,
                     source="watchlist")
    child = _seed(conn, "official-new", "美团金融", "AI 工程师-新需求",
                  Status.DUPLICATE, source="watchlist")
    db.update_job_fields(conn, selected, url="https://zhaopin.meituan.com/web/position/detail?jobUnionId=111")
    db.update_job_fields(conn, child, url="https://zhaopin.meituan.com/web/position/detail?jobUnionId=222")
    assert selected != child
    with pytest.raises(decide.DuplicateConfirmationRequired) as error:
        decide.requeue(conn, child)
    assert error.value.allow_override is True
    decide.requeue(conn, child, confirm_distinct=True)
    assert db.get_job(conn, child)["status"] == Status.QUEUED
    row = next(item for item in decide.queue_rows(conn) if item["job_id"] == child)
    assert not row["blocking_approved"]

    newly_selected = _seed(conn, "third", "美团", "AI 工程师-另一张帖", Status.APPROVED,
                           source="watchlist")
    assert newly_selected not in (selected, child)
    assert decide.queue_rows(conn)[0]["blocking_approved"][0]["related_job_id"] == newly_selected


def test_different_boss_listing_ids_do_not_prove_distinct_official_requisitions(conn):
    selected = _seed(conn, "boss-old", "快手", "算法岗位", Status.APPROVED, source="boss")
    child = _seed(conn, "boss-new", "北京快手科技有限公司", "算法岗位-新帖",
                  Status.DUPLICATE, source="boss")
    db.update_job_fields(conn, selected, url="https://www.zhipin.com/job_detail/abc123.html")
    db.update_job_fields(conn, child, url="https://www.zhipin.com/job_detail/def456.html")
    with pytest.raises(decide.DuplicateConfirmationRequired) as error:
        decide.requeue(conn, child)
    assert error.value.allow_override is False


def test_new_selection_requires_confirmation_and_logs_keep_both(conn):
    first = _seed(conn, "one", "北京快手科技有限公司", "多模态研究员", Status.APPROVED)
    second = _seed(conn, "two", "快手", "Agent 研究员", Status.PENDING_REVIEW)
    with pytest.raises(decide.DuplicateConfirmationRequired):
        decide.apply(conn, second, "apply")
    assert db.get_job(conn, second)["status"] == Status.PENDING_REVIEW
    decide.apply(conn, second, "apply", confirm_duplicate=True)
    assert db.get_job(conn, second)["status"] == Status.APPROVED
    row = conn.execute("SELECT action, related_job_id FROM duplicate_reviews WHERE job_id=?", (second,)).fetchone()
    assert (row["action"], row["related_job_id"]) == ("keep_both", first)


def test_existing_selected_pair_can_be_kept_or_manually_moved(conn):
    first = _seed(conn, "one", "快手", "多模态研究员", Status.APPROVED)
    second = _seed(conn, "two", "北京快手科技有限公司", "Agent 研究员", Status.APPROVED)
    assert len(decide.approved_duplicate_pairs(conn)) == 1
    decide.review_approved_duplicate(conn, second, first, "keep_both")
    assert decide.approved_duplicate_pairs(conn)[0]["review"]["action"] == "keep_both"
    decide.review_approved_duplicate(conn, second, first, "mark_duplicate")
    assert db.get_job(conn, first)["status"] == Status.APPROVED
    moved = db.get_job(conn, second)
    assert moved["status"] == Status.DUPLICATE
    assert json.loads(moved["raw_json"])["dup_of"] == first
    assert conn.execute("SELECT COUNT(*) FROM duplicate_reviews WHERE job_id=?", (second,)).fetchone()[0] == 2


def test_board_requeue_redirects_to_visible_queue_and_confirmation(tmp_path):
    path = tmp_path / "jobs.sqlite"
    conn = db.connect(path)
    db.init_db(conn, ROOT / "db" / "schema.sql")
    selected = _seed(conn, "bili", "哔哩哔哩", "AI 应用开发实习", Status.APPROVED)
    child = _seed(conn, "pdd", "拼多多集团-PDD", "AI 应用开发", Status.DUPLICATE)
    peer = _seed(conn, "pdd-peer", "拼多多", "AI 应用开发同岗", Status.DUPLICATE)
    conn.close()
    app = create_app(path, ROOT / "db" / "schema.sql")
    app.config["TESTING"] = True
    client = app.test_client()

    response = client.post("/requeue/" + child, data={"return_to": "/prescreened?company=拼多多"})
    assert response.location.startswith("/queue?company=") and "#job-" + child in response.location
    html = client.get(response.location).get_data(as_text=True)
    assert "待分析队列" in html and child in html and "哔哩哔哩" in html

    conn = db.connect(path)
    db.set_status(conn, peer, Status.PENDING_REVIEW)
    db.set_status(conn, child, Status.APPROVED)
    conn.close()
    confirmation = client.post("/decide/" + peer, data={"decision": "apply"})
    assert confirmation.status_code == 200
    assert "确认选择相似岗位" in confirmation.get_data(as_text=True)
    selected_page = client.get("/selected/duplicates")
    assert selected_page.status_code == 200 and "已选岗位重复审查" in selected_page.get_data(as_text=True)
