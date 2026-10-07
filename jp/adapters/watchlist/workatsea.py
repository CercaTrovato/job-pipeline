from __future__ import annotations
from typing import Any, Callable, Dict, List

from jp.adapters.base import RateLimiter, html_to_text
from jp.adapters.watchlist.registry import limit, register
from jp.models import RawJob

API = "https://ats.workatsea.com/ats/api/v1/user"
PAGE = 500


@register("workatsea")
def fetch_company(entry, http, limits: Dict[str, Any], limiter: RateLimiter, log: Callable[[str], None]) -> List[RawJob]:
    """Shopee 自建 ATS：无服务端过滤，拉全量后按 params.city_ids 本地筛（深圳 city_id=6）。"""
    raw_ids = entry.params.get("city_ids")
    city_ids = {int(x) for x in (raw_ids if raw_ids is not None else [6])}
    max_pages = limit(entry, limits, "max_pages", 3)
    limiter.wait()
    meta = http.get_json(API + "/meta/slice/?flags=2147851120&from_career=true") or {}
    cities = {int(c["city_id"]): c for c in ((meta.get("data") or {}).get("flat_locations") or []) if c.get("city_id") is not None}
    out: List[RawJob] = []
    offset = 0
    for _ in range(max_pages):
        limiter.wait()
        d = http.get_json("%s/job/list/?limit=%d&offset=%d" % (API, PAGE, offset)) or {}
        data = d.get("data") or {}
        rows = data.get("job_list") or []
        for r in rows:
            cid = int(r.get("city_id") or 0)
            if cid not in city_ids:
                continue
            c = cities.get(cid) or {}
            loc = ", ".join(x for x in (str(c.get("city_name") or ""), str(c.get("region_name") or "")) if x)
            jd = "\n".join(x for x in (html_to_text(r.get("job_description") or ""), html_to_text(r.get("requirements") or "")) if x)
            out.append(RawJob(
                platform_id="workatsea:%s" % (r.get("job_id") or r.get("id")),
                title=str(r.get("job_name") or ""),
                company=entry.company,
                url="https://careers.shopee.sg/jobs?keyword=%s" % (r.get("job_id") or r.get("id")),
                jd_text=jd,
                location=loc,
                salary_raw="",
                posted_at="",
                raw={"employment_id": r.get("employment_id"), "city_id": cid, "department_id": r.get("department_id"),
                     "job_type_id": r.get("job_type_id"), "internal_id": r.get("id")},
            ))
        offset += len(rows)
        if not rows or len(rows) < PAGE or offset >= int(data.get("total_count") or 0):
            break
    log("workatsea %s：命中城市 %d 条（扫描 %d）" % (entry.company, len(out), offset))
    return out
