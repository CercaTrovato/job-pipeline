from __future__ import annotations
from typing import Any, Callable, Dict, List

from jp.adapters.base import RateLimiter, html_to_text, keyword_hit
from jp.adapters.watchlist.registry import limit, register
from jp.models import RawJob


@register("pinpoint")
def fetch_company(entry, http, limits: Dict[str, Any], limiter: RateLimiter, log: Callable[[str], None]) -> List[RawJob]:
    """Pinpoint：GET https://<sub>.pinpointhq.com/postings.json 一次拿全部（含 JD HTML）。"""
    sub = entry.params["sub"]
    detail_limit = limit(entry, limits, "detail_limit", 30)
    limiter.wait()
    data = (http.get_json("https://%s.pinpointhq.com/postings.json" % sub) or {}).get("data") or []
    out: List[RawJob] = []
    for p in data:
        probe = " ".join(str(p.get(k) or "") for k in ("title", "employment_type_text", "employment_type"))
        if not (keyword_hit(probe, entry.keywords) or "intern" in probe.lower()):
            continue
        jd = "\n".join(x for x in (html_to_text(p.get("description") or ""), html_to_text(p.get("key_responsibilities") or ""),
                                   html_to_text(p.get("skills_knowledge_expertise") or "")) if x)
        out.append(RawJob(
            platform_id="pinpoint:%s:%s" % (sub, p.get("id")),
            title=str(p.get("title") or ""),
            company=entry.company,
            url=p.get("url") or "https://%s.pinpointhq.com%s" % (sub, p.get("path") or ""),
            jd_text=jd,
            location=str((p.get("location") or {}).get("name") or ""),
            salary_raw=str(p.get("compensation") or ""),
            posted_at="",
            raw={"employment_type": p.get("employment_type"), "workplace_type": p.get("workplace_type"),
                 "deadline_at": p.get("deadline_at") or ""},
        ))
    log("pinpoint %s：%d 个职位，命中 %d" % (entry.company, len(data), len(out)))
    return out[:detail_limit]
