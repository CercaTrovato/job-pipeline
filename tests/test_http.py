from __future__ import annotations
import io
import json
import urllib.error
import pytest
from jp.http import HttpClient
from jp.adapters.base import AdapterError, RiskControlError


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body.encode("utf-8")
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, table):
        self.table, self.seen = table, []
    def open(self, req, timeout=None):
        self.seen.append((req.get_method(), req.full_url, req.data, dict(req.header_items())))
        item = self.table[req.full_url]
        if isinstance(item, int):
            raise urllib.error.HTTPError(req.full_url, item, "err", {}, io.BytesIO(b"denied"))
        return _Resp(200, item)


def test_get_json_and_post_json_send_ua_and_body():
    op = FakeOpener({"https://x/a": '{"ok": 1}', "https://x/b": '{"echo": true}'})
    c = HttpClient(); c._opener = op
    assert c.get_json("https://x/a") == {"ok": 1}
    assert c.post_json("https://x/b", {"k": "值"}, headers={"X-T": "1"}) == {"echo": True}
    m, url, data, hdr = op.seen[1]
    assert m == "POST" and json.loads(data.decode("utf-8")) == {"k": "值"}
    assert hdr["User-agent"].startswith("Mozilla/5.0") and hdr["X-t"] == "1" and hdr["Content-type"] == "application/json"


def test_403_429_become_risk_and_other_http_errors_become_adapter_error():
    c = HttpClient(); c._opener = FakeOpener({"https://x/403": 403, "https://x/429": 429, "https://x/500": 500})
    with pytest.raises(RiskControlError):
        c.get_text("https://x/403")
    with pytest.raises(RiskControlError):
        c.get_text("https://x/429")
    with pytest.raises(AdapterError):
        c.get_text("https://x/500")


def test_non_json_response_is_adapter_error():
    c = HttpClient(); c._opener = FakeOpener({"https://x/html": "<html>"})
    with pytest.raises(AdapterError):
        c.get_json("https://x/html")
