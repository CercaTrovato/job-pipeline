from __future__ import annotations
from typing import Any, Callable, Dict, List

from jp.adapters.base import RateLimiter, html_to_text, keyword_hit
from jp.adapters.watchlist.registry import limit, register
from jp.models import RawJob


def _location(loc: Dict[str, Any]) -> str:
    return ", ".join(x for x in (str((loc or {}).get("city") or ""), str((loc or {}).get("state") or "")) if x and x != "N/A")


@register("bamboohr")
def fetch_company(entry, http, limits: Dict[str, Any], limiter: RateLimiter, log: Callable[[str], None]) -> List[RawJob]:
    """BambooHR：GET /careers/list（全部在岗职位）→ 标题命中关键词或雇佣类型含 intern 的 → GET /careers/<id>/detail。"""
    sub = entry.params["sub"]
    base = "https://%s.bamboohr.com" % sub
    detail_limit = limit(entry, limits, "detail_limit", 30)
    limiter.wait()
    rows = (http.get_json(base + "/careers/list") or {}).get("result") or []
    picks = [r for r in rows if keyword_hit(str(r.get("jobOpeningName") or ""), entry.keywords)
             or "intern" in str(r.get("employmentStatusLabel") or "").lower()]
    log("bamboohr %s：%d 个职位，标题命中 %d" % (entry.company, len(rows), len(picks)))
    out: List[RawJob] = []
    for r in picks[:detail_limit]:
        limiter.wait()
        jo = ((http.get_json("%s/careers/%s/detail" % (base, r["id"])) or {}).get("result") or {}).get("jobOpening") or {}
        out.append(RawJob(
            platform_id="bamboohr:%s:%s" % (sub, r["id"]),
            title=jo.get("jobOpeningName") or r.get("jobOpeningName", ""),
            company=entry.company,
            url=jo.get("jobOpeningShareUrl") or "%s/careers/%s" % (base, r["id"]),
            jd_text=html_to_text(jo.get("description") or ""),
            location=_location(jo.get("location") or r.get("location") or {}),
            salary_raw=str(jo.get("compensation") or ""),
            posted_at=str(jo.get("datePosted") or ""),
            raw={"employment": jo.get("employmentStatusLabel") or r.get("employmentStatusLabel", ""),
                 "department": r.get("departmentLabel", "")},
        ))
    return out
