from __future__ import annotations
import urllib.parse
from typing import Any, Callable, Dict, List

from jp.adapters.base import AdapterError, RateLimiter, ms_to_date
from jp.adapters.watchlist.registry import limit, register
from jp.models import RawJob

PAGE = 50
_LIST_KEYS = ("job_category_id_list", "tag_id_list", "location_code_list", "subject_id_list",
              "recruitment_id_list", "job_function_id_list", "storefront_id_list")


def to_rawjob(entry, base: str, site: str, x: Dict[str, Any]) -> RawJob:
    host = entry.params["host"]
    cities = [str(c.get("name") or "") for c in (x.get("city_list") or []) if c.get("name")]
    parts: List[str] = []
    if x.get("description"):
        parts.append("岗位描述：\n%s" % x["description"])
    if x.get("requirement"):
        parts.append("任职要求：\n%s" % x["requirement"])
    return RawJob(
        platform_id="feishu:%s:%s" % (host, x.get("id")),
        title=str(x.get("title") or ""),
        company=entry.company,
        url="%s/%s/position/%s/detail" % (base, site, x.get("id")),
        jd_text="\n".join(parts),
        location="，".join(cities),
        salary_raw="",
        posted_at=ms_to_date(x.get("publish_time")),
        raw={"recruit_type": (x.get("recruit_type") or {}).get("name", ""),
             "category": (x.get("job_category") or {}).get("name", ""), "code": x.get("code")},
    )


@register("feishu")
def fetch_company(entry, http, limits: Dict[str, Any], limiter: RateLimiter, log: Callable[[str], None]) -> List[RawJob]:
    """飞书招聘门户：csrf token → search/job/posts 翻页（keyword 留空拿全量，关键词初筛交给 run_watchlist）。"""
    p = entry.params
    host, site, ptype = p["host"], str(p.get("site", "index")), int(p.get("portal_type", 2))
    base = "https://" + host
    max_pages = limit(entry, limits, "max_pages", 3)
    hdr = {"Portal-Channel": "saas-career", "Portal-Platform": "pc", "accept-language": "zh-CN",
           "website-path": site, "Referer": "%s/%s/position/list" % (base, site), "Origin": base}
    limiter.wait()
    tok = (((http.post_json(base + "/api/v1/csrf/token", {}, hdr) or {}).get("data") or {}).get("token")) or ""
    hdr = dict(hdr)
    hdr["x-csrf-token"] = tok
    out: List[RawJob] = []
    for n in range(max_pages):
        limiter.wait()
        query = {"keyword": "", "limit": PAGE, "offset": n * PAGE, "portal_type": ptype, "portal_entrance": 1}
        query.update({k: "" for k in _LIST_KEYS})
        body = {"keyword": "", "limit": PAGE, "offset": n * PAGE, "portal_type": ptype, "portal_entrance": 1}
        body.update({k: [] for k in _LIST_KEYS})
        d = http.post_json(base + "/api/v1/search/job/posts?" + urllib.parse.urlencode(query), body, hdr) or {}
        if d.get("code") != 0:
            raise AdapterError("feishu %s（%s）code=%s %s" % (host, site, d.get("code"), d.get("message")))
        data = d.get("data") or {}
        posts = data.get("job_post_list") or []
        out.extend(to_rawjob(entry, base, site, x) for x in posts)
        count = int(data.get("count") or 0)
        if not posts or (n + 1) * PAGE >= count:
            break
    log("feishu %s：%d 条" % (entry.company, len(out)))
    return out
