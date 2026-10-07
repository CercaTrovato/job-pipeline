from __future__ import annotations
from tests.test_board import client  # noqa: F401  复用 fixture

def test_index_has_score_ring_and_tokens(client):
    cl, jid = client
    html = cl.get("/").get_data(as_text=True)
    assert "--accent" in html and "score-ring" in html and "<svg" in html
    assert 'class="nav-link active"' in html
    assert "88.0" in html and jid in html

def test_job_detail_has_grade_badges(client):
    cl, jid = client
    html = cl.get("/job/%s" % jid).get_data(as_text=True)
    assert 'class="grade grade-A"' in html and "req-level-high" in html

def test_nav_counts(client):
    cl, _ = client
    html = cl.get("/runs").get_data(as_text=True)
    assert "待挑选 <span class=\"count\">1</span>" in html
    assert "选择投递" in cl.get("/").get_data(as_text=True)


def test_job_detail_shows_score_breakdown(client):
    cl, jid = client
    html = cl.get("/job/%s" % jid).get_data(as_text=True)
    assert "评分明细" in html and 'class="breakdown"' in html and "贡献" in html
