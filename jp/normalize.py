from __future__ import annotations
import hashlib
import re
from typing import Optional

_COMPANY_SUFFIX = re.compile(
    r"(股份有限公司|有限责任公司|有限公司|集团|公司|"
    r"co\.?,?\s*ltd\.?|company\s+limited|limited|ltd\.?|inc\.?|corp\.?|corporation|llc|group|holdings?)\s*$",
    re.IGNORECASE,
)
_BRACKETS = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]")
_WS = re.compile(r"\s+")
_HANT_CHARS = set("們實習負責數據處理應該開發與網絡學習體驗國際電腦軟體資訊關於進階團隊項目專業經驗優先權益資料")


def _clean(s: str) -> str:
    return _WS.sub(" ", (s or "").strip()).lower()


def norm_company(s: str) -> str:
    cleaned = _clean(s)
    s = cleaned
    prev = None
    while prev != s:
        prev = s
        s = _COMPANY_SUFFIX.sub("", s).strip(" .,，。")
    return s if s else cleaned


def norm_title(s: str) -> str:
    s = _BRACKETS.sub("", s or "")
    return _clean(s)


def norm_location(s: str) -> str:
    s = _clean(s)
    if not s:
        return ""
    s = re.split(r"[·,，/|\-–]", s)[0].strip()
    s = re.sub(r"(市|区|區)$", "", s)
    return s


def fingerprint(company: str, title: str, location: str) -> str:
    key = "|".join([norm_company(company), norm_title(title), norm_location(location)])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def make_job_id(source: str, platform_id: Optional[str], company: str, title: str, location: str) -> str:
    if platform_id:
        key = "%s:%s" % (source, platform_id)
    else:
        key = "%s:%s" % (source, fingerprint(company, title, location))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def detect_lang(text: str) -> str:
    text = text or ""
    han = sum(1 for ch in text if "一" <= ch <= "鿿")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if han == 0 and latin > 0:
        return "en"
    if han < latin / 4:
        return "en"
    hant = sum(1 for ch in text if ch in _HANT_CHARS)
    return "zh-hant" if hant >= 3 else "zh"
