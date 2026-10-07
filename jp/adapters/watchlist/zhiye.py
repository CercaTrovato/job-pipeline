from __future__ import annotations
from typing import Any, Callable, Dict, List

from jp.adapters.base import AdapterError, RateLimiter
from jp.adapters.watchlist.registry import limit, register
from jp.models import RawJob

PAGE = 20


def to_rawjob(entry, base: str, r: Dict[str, Any]) -> RawJob:
    host = entry.params["host"]
    parts: List[str] = []
    if r.get("Duty"):
        parts.append("岗位职责：\n%s" % r["Duty"])
    if r.get("Require"):
        parts.append("任职要求：\n%s" % r["Require"])
    posted = str(r.get("PostDate") or "")[:10]
    return RawJob(
        platform_id="beisen:%s:%s" % (host, r.get("JobAdId")),
        title=str(r.get("JobAdName") or ""),
        company=entry.company,
        url="%s/campus/jobs#jobAdId=%s" % (base, r.get("JobAdId")),
        jd_text="\n".join(parts),
        location="，".join(str(x) for x in (r.get("LocNames") or []) if x),
        salary_raw=str(r.get("Salary") or ""),
        posted_at="" if posted.startswith("0001") else posted,
        raw={"category": r.get("Category"), "kind": r.get("Kind"), "job_ad_id": r.get("JobAdId")},
    )


@register("beisen")
def fetch_company(entry, http, limits: Dict[str, Any], limiter: RateLimiter, log: Callable[[str], None]) -> List[RawJob]:
    """北森 zhiye 门户：POST /api/Jobad/GetJobAdPageList 翻页（列表自带 Duty / Require）。"""
    host = entry.params["host"]
    base = "https://" + host
    cat = int(entry.params.get("category_id", 2))
    max_pages = limit(entry, limits, "max_pages", 3)
    hdr = {"Referer": base + "/campus/jobs", "Origin": base}
    out: List[RawJob] = []
    d: Dict[str, Any] = {}
    for n in range(1, max_pages + 1):
        limiter.wait()
        d = http.post_json(base + "/api/Jobad/GetJobAdPageList", {"PageIndex": n, "PageSize": PAGE, "Keyword": "", "CategoryId": cat}, hdr) or {}
        if d.get("Code") != 200:
            raise AdapterError("zhiye %s Code=%s %s" % (host, d.get("Code"), d.get("Message")))
        rows = d.get("Data") or []
        out.extend(to_rawjob(entry, base, r) for r in rows)
        if len(rows) < PAGE:
            break
    log("zhiye %s：%d 条（Count=%s）" % (entry.company, len(out), d.get("Count")))
    return out
