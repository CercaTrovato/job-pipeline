from __future__ import annotations
import pathlib
import pytest
from jp import db as jpdb

ROOT = pathlib.Path(__file__).resolve().parents[1]

@pytest.fixture
def conn(tmp_path):
    c = jpdb.connect(tmp_path / "t.sqlite")
    jpdb.init_db(c, ROOT / "db" / "schema.sql")
    yield c
    c.close()
