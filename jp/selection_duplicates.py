"""只读检测已选岗位与待选岗位的重复候选。"""
from __future__ import annotations

import difflib
import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from jp import normalize as n
from jp.rules import hard

MIN_EXACT_JD_CHARS = 150
NEAR_JD_RATIO = 0.90

_BRANDS = (
    ("京东方", re.compile(r"京东方|京東方|\bboe\b", re.I)),
    ("京东", re.compile(r"京东|京東|\bjd(?:\.com)?\b", re.I)),
    ("拼多多", re.compile(r"拼多多|拼多多集团|\bpinduoduo\b|\bpdd\b", re.I)),
    ("temu", re.compile(r"\btemu\b", re.I)),
    ("华为", re.compile(r"华为|華為|\bhuawei\b", re.I)),
    ("快手", re.compile(r"快手|\bkuaishou\b", re.I)),
    ("binance", re.compile(r"\bbinance\b", re.I)),
    ("美团", re.compile(r"美团|美團|\bmeituan\b", re.I)),
    ("小红书", re.compile(r"小红书|小紅書|行吟|\bxiaohongshu\b|\brednote\b", re.I)),
)
_LEGAL_SUFFIX = re.compile(
    r"(有限责任公司|股份有限公司|有限公司|集团有限公司|集团|公司|"
    r"company limited|co\.?\s*ltd\.?|limited|ltd\.?|inc\.?|corp\.?|corporation|llc|group|holdings?)$",
    re.I,
)
_COMPANY_NOISE = re.compile(r"[\s\-—_.,，。()（）【】\[\]·]+")
_TITLE_BRACKETS = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]")


def brand_key(company: str) -> str:
    """将已知招聘品牌归一；未知公司名仅移除法定主体后缀和格式差异。"""
    value = (company or "").strip()
    for key, pattern in _BRANDS:
        if pattern.search(value):
            return key
    value = _COMPANY_NOISE.sub("", value).casefold()
    previous = None
    while previous != value:
        previous = value
        value = _LEGAL_SUFFIX.sub("", value).rstrip(" .")
    return value


def normalized_jd(text: str) -> str:
    """移除 JD 中全部空白并统一大小写，不截断正文。"""
    return re.sub(r"\s+", "", text or "").casefold()


def _get(row: Any, key: str, default: Any = "") -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _raw(row: Any) -> Dict[str, Any]:
    raw = _get(row, "raw_json", "")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _city(row: Any) -> str:
    values = re.split(r"[，,、/;；|]", str(_get(row, "location", "") or ""))
    cities = set()
    for value in values:
        city = n.norm_location(value)
        city = re.sub(r"(?:市|区|區)$", "", re.sub(r"\s+", "", city)).casefold()
        if city:
            cities.add(city)
    return "|".join(sorted(cities))


def _track(row: Any) -> str:
    raw = _raw(row)
    explicit = raw.get("track")
    if explicit in ("intern", "campus"):
        return explicit
    return hard.detect_track(
        str(_get(row, "title", "") or ""),
        str(_get(row, "jd_text", "") or ""),
        str(raw.get("recruit_type") or ""),
        internship_fields=bool(raw.get("days_per_week") or raw.get("min_months")),
        url=str(_get(row, "url", "") or ""),
    )


def _title(row: Any) -> str:
    return re.sub(r"\s+", "", n.norm_title(str(_get(row, "title", "") or ""))).casefold()


def _official_ids(row: Any) -> List[Tuple[str, str]]:
    """从常见招聘源 URL / 原始字段提取有命名空间的职位 ID。"""
    url = str(_get(row, "url", "") or "")
    source = str(_get(row, "source", "") or "").casefold()
    ids: List[Tuple[str, str]] = []
    parsed = urlparse(url)
    host = parsed.netloc.casefold()
    path = parsed.path
    query = parse_qs(parsed.query)
    if "meituan.com" in host:
        for value in query.get("jobUnionId", []):
            ids.append(("meituan", value))
    if "zhipin.com" in host:
        match = re.search(r"/job_detail/([A-Za-z0-9_~-]+)\.html", path, re.I)
        if match:
            ids.append(("boss", match.group(1).casefold()))
    if "nowcoder.com" in host:
        match = re.search(r"/jobs/detail/([^/?#]+)", path, re.I)
        if match:
            ids.append(("nowcoder", match.group(1).casefold()))
    if "linkedin.com" in host:
        match = re.search(r"/jobs/view/(\d+)", path, re.I)
        if match:
            ids.append(("linkedin", match.group(1)))
    raw = _raw(row)
    for key in ("jobUnionId", "requisitionId", "requisition_id", "official_job_id"):
        value = raw.get(key)
        if value not in (None, ""):
            namespace = "meituan" if key == "jobUnionId" else source or "official"
            ids.append((namespace, str(value).casefold()))
    return list(dict.fromkeys(ids))


def employer_requisition_ids(row: Any) -> List[Tuple[str, str]]:
    """Employer-owned requisition IDs; platform posting IDs are deliberately excluded."""
    parsed = urlparse(str(_get(row, "url", "") or ""))
    host = parsed.netloc.casefold()
    ids: List[Tuple[str, str]] = []
    if "meituan.com" in host:
        ids += [("meituan", value) for value in parse_qs(parsed.query).get("jobUnionId", [])]
    if "mokahr.com" in host:
        match = re.search(r"(?:^|/)job/([a-f0-9-]{8,})", parsed.fragment, re.I)
        if match:
            ids.append(("moka:" + parsed.path.casefold(), match.group(1).casefold()))
    if "jobs.bytedance.com" in host:
        match = re.search(r"/position/(\d+)/detail", parsed.path, re.I)
        if match:
            ids.append(("bytedance", match.group(1)))
    raw = _raw(row)
    if str(_get(row, "source", "") or "") == "watchlist" and raw.get("watchlist"):
        value = raw.get("official_job_id") or _get(row, "platform_id", "")
        if value and host and not any(domain in host for domain in ("zhipin.com", "nowcoder.com", "linkedin.com")):
            ids.append(("watchlist:" + host, str(value).casefold()))
    return list(dict.fromkeys(ids))


def _base_compatible(a: Any, b: Any) -> bool:
    region_a = str(_get(a, "region", "") or "")
    region_b = str(_get(b, "region", "") or "")
    return (
        brand_key(str(_get(a, "company", "") or ""))
        == brand_key(str(_get(b, "company", "") or ""))
        and (not region_a or not region_b or region_a == region_b)
        and _track(a) == _track(b)
    )


def _city_relation(a: Any, b: Any) -> Tuple[bool, bool]:
    cities_a, cities_b = set(filter(None, _city(a).split("|"))), set(filter(None, _city(b).split("|")))
    return bool(cities_a and cities_b and cities_a.intersection(cities_b)), not cities_a and not cities_b


def compare_rows(a: Any, b: Any) -> Optional[dict]:
    """比较两条岗位；不同招聘品牌永不返回冲突。"""
    brand_a = brand_key(str(_get(a, "company", "") or ""))
    if not brand_a or brand_a != brand_key(str(_get(b, "company", "") or "")):
        return None
    if not _base_compatible(a, b):
        return None
    same_city, both_missing_city = _city_relation(a, b)
    if not (same_city or both_missing_city):
        return None
    a_ids, b_ids = set(_official_ids(a)), set(_official_ids(b))
    shared_ids = sorted(a_ids & b_ids)
    if shared_ids:
        label = "相同平台岗位编号" if shared_ids[0][0] in ("boss", "nowcoder", "linkedin") else "相同官方职位编号"
        return {
            "related_job_id": str(_get(b, "job_id", "")),
            "kind": "exact",
            "similarity": 1.0,
            "reason": "%s：%s:%s" % (label, *shared_ids[0]),
        }
    left, right = normalized_jd(str(_get(a, "jd_text", "") or "")), normalized_jd(str(_get(b, "jd_text", "") or ""))
    if (same_city or both_missing_city) and len(left) >= MIN_EXACT_JD_CHARS and left == right:
        return {
            "related_job_id": str(_get(b, "job_id", "")),
            "kind": "exact",
            "similarity": 1.0,
            "reason": "同品牌、同地区、同轨道，完整 JD 去空白后完全一致",
        }
    if (same_city and len(left) >= MIN_EXACT_JD_CHARS and len(right) >= MIN_EXACT_JD_CHARS
            and _title(a) and _title(a) == _title(b)):
        similarity = difflib.SequenceMatcher(None, left, right).ratio()
        if similarity >= NEAR_JD_RATIO:
            return {
                "related_job_id": str(_get(b, "job_id", "")),
                "kind": "near",
                "similarity": round(similarity, 4),
                "reason": "同品牌、同地区、同轨道、同标题，JD 相似度达到 0.90",
            }
    return None


def find_approved_conflicts(conn: Any, job_row: Any) -> List[dict]:
    """只读查询 approved 岗位，并返回与 job_row 冲突的候选清单。"""
    candidates = conn.execute(
        "SELECT job_id, source, region, platform_id, company, title, location, url, jd_text, raw_json "
        "FROM jobs WHERE status = ? ORDER BY job_id",
        ("approved",),
    ).fetchall()
    conflicts = []
    for row in candidates:
        if str(_get(row, "job_id", "")) == str(_get(job_row, "job_id", "")):
            continue
        conflict = compare_rows(job_row, row)
        if conflict:
            conflicts.append(conflict)
    return conflicts
