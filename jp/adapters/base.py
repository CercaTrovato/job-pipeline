from __future__ import annotations
import random
import re
import time
from dataclasses import dataclass, field
from html import unescape
from typing import Any, Callable, Dict, List, Optional

from jp.models import RawJob


class AdapterError(Exception):
    """适配器可预期的失败（网络、解析、平台返回错误）。fetch 记进 runs.last_error 后继续下一个来源。"""


class AuthRequiredError(AdapterError):
    """登录态缺失：只提示用户到 Edge 登录，绝不自动登录。"""


class RiskControlError(AdapterError):
    """验证码 / 限流 / 账号异常迹象：哨兵据此冷却 24 h，绝不绕过。"""


RISK_RE = re.compile(
    r"验证码|安全验证|操作频繁|账号异常|投递上限|请稍后再试|captcha|unusual activity|verify (that )?you\b|too many requests|rate limit",
    re.I,
)


def looks_like_risk(text: str) -> bool:
    return bool(RISK_RE.search(text or ""))


@dataclass
class SearchQuery:
    region: str                     # "CN" | "HK"
    keywords: List[str]             # 每个关键词各搜一轮
    city: str = ""                  # Boss / 牛客 的城市名；LinkedIn / JobsDB 的 location 文本
    max_pages: int = 3
    page_size: int = 15
    detail_limit: int = 40          # 单次会话最多抓多少条详情
    extra: Dict[str, Any] = field(default_factory=dict)   # 平台特有参数（experience / job_type / experience_level / date_posted …）


class RateLimiter:
    """相邻两次请求之间随机等待 [min_s, max_s] 秒；第一次不等。sleep / rand 可注入以便测试。"""

    def __init__(self, min_s: float, max_s: float,
                 sleep: Callable[[float], None] = time.sleep,
                 rand: Callable[[float, float], float] = random.uniform):
        self.min_s, self.max_s = min_s, max_s
        self._sleep, self._rand = sleep, rand
        self.calls = 0

    def wait(self) -> None:
        if self.calls > 0:
            self._sleep(self._rand(self.min_s, self.max_s))
        self.calls += 1


_BREAK_RE = re.compile(r"<(br|/p|/div|/li|/tr|/h[1-6]|/ul|/ol|/blockquote)\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(html: str) -> str:
    """HTML → 纯文本：换行/块级闭合标签变换行，其它标签删掉，实体解码，连续空行压成一行。"""
    if not html:
        return ""
    s = _BREAK_RE.sub("\n", html)
    s = _TAG_RE.sub("", s)
    s = unescape(s).replace("\xa0", " ")
    out: List[str] = []
    blank = 0
    for ln in s.splitlines():
        ln = re.sub(r"[ \t]+", " ", ln).strip()
        if ln:
            out.append(ln)
            blank = 0
        else:
            blank += 1
            if blank == 1 and out:
                out.append("")
    return "\n".join(out).strip()


def keyword_hit(text: str, keywords: List[str]) -> bool:
    """任一关键词（大小写不敏感）出现即命中；关键词列表为空视为命中。
    ≤3 个字符的纯 ASCII 关键词（AI / ML / NLP）按整词匹配，避免 'ai' 命中 'Maintenance'；其它按子串。"""
    if not keywords:
        return True
    t = (text or "").lower()
    for k in keywords:
        k = (k or "").strip().lower()
        if not k:
            continue
        if k.isascii() and len(k) <= 3:
            if re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(k), t):
                return True
        elif k in t:
            return True
    return False


def ms_to_date(ms: Optional[int]) -> str:
    """毫秒时间戳 → 'YYYY-MM-DD'（UTC）；空值返回空串。"""
    if not ms:
        return ""
    return time.strftime("%Y-%m-%d", time.gmtime(int(ms) / 1000))


def split_for_detail(rows: Dict[str, Any], detail_limit: int, known_ids: Optional[Any]):
    """列表行按 platform_id 分三类：要抓详情的（未入库，按列表顺序最多 detail_limit 条）、已入库的（只回列表行让
    ingest 记"已见"）、超预算的条数（不返回，下次 fetch 时它们仍是未知的，会轮到）。"""
    known = set(known_ids or ())
    fetch, seen, over = [], [], 0
    for pid, r in rows.items():
        if pid in known:
            seen.append((pid, r))
        elif len(fetch) < detail_limit:
            fetch.append((pid, r))
        else:
            over += 1
    return fetch, seen, over


class Adapter:
    """平台适配器接口：只抓数据，不做分析、不碰 SQLite。"""
    source: str = ""
    region: str = ""

    def search(self, q: SearchQuery, limiter: RateLimiter, log: Callable[[str], None] = print) -> List[RawJob]:
        raise NotImplementedError
