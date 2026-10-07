from __future__ import annotations
import http.cookiejar
import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from jp.adapters.base import AdapterError, RiskControlError

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"


class HttpClient:
    """纯 HTTP 适配器共用的薄封装：urllib + cookie jar，无第三方依赖。
    403 / 429 → RiskControlError（哨兵冷却）；其它 HTTP 错误与网络错误 → AdapterError。"""

    def __init__(self, timeout: int = 30, ua: str = UA):
        self.timeout, self.ua = timeout, ua
        self.jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, url: str, data: Optional[bytes] = None,
                headers: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
        h = {"User-Agent": self.ua, "Accept": "application/json, text/plain, */*"}
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            if e.code in (403, 429):
                raise RiskControlError("HTTP %d %s" % (e.code, url))
            raise AdapterError("HTTP %d %s %s" % (e.code, url, body))
        except (urllib.error.URLError, OSError) as e:
            raise AdapterError("网络错误 %s: %s" % (url, e))

    def get_text(self, url: str, headers: Optional[Dict[str, str]] = None) -> str:
        return self.request("GET", url, None, headers)[1]

    def get_json(self, url: str, headers: Optional[Dict[str, str]] = None) -> Any:
        return _loads(self.get_text(url, headers), url)

    def post_json(self, url: str, payload: Any, headers: Optional[Dict[str, str]] = None) -> Any:
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return _loads(self.request("POST", url, body, h)[1], url)


def _loads(text: str, url: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        raise AdapterError("非 JSON 响应 %s: %s" % (url, text[:120]))
