from __future__ import annotations
import pathlib
import pytest
from board.app import create_app
from jp import db as jpdb, sentinel

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def app_db(tmp_path):
    db = tmp_path / "b.sqlite"
    c = jpdb.connect(db)
    jpdb.init_db(c, ROOT / "db" / "schema.sql")
    yield db, c
    c.close()


def test_banner_absent_without_cooldown(app_db):
    db, c = app_db
    app = create_app(db, ROOT / "db" / "schema.sql"); app.config["TESTING"] = True
    html = app.test_client().get("/").get_data(as_text=True)
    assert "风控冷却中" not in html


def test_banner_shows_active_cooldowns_on_every_page(app_db):
    db, c = app_db
    sentinel.start_cooldown(c, "boss", "疑似风控：安全验证", hours=24)
    app = create_app(db, ROOT / "db" / "schema.sql"); app.config["TESTING"] = True
    cl = app.test_client()
    for path in ("/", "/runs", "/prescreened", "/referral"):
        html = cl.get(path).get_data(as_text=True)
        assert "风控冷却中" in html and "boss" in html and "安全验证" in html, path
    assert 'class="alert"' in cl.get("/").get_data(as_text=True)
