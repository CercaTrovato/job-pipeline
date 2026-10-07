from __future__ import annotations
from typing import Any, Callable, Dict, List, Tuple

from jp.adapters.base import RateLimiter, html_to_text
from jp.adapters.watchlist.registry import limit, register
from jp.models import RawJob

PAGE = 20


def _region_facet(facets: List[Dict[str, Any]], region_re) -> Dict[str, List[str]]:
    """在名字含 location 的 facet 里找描述匹配地区的值 → {facetParameter: [id]}；找不到返回 {}。"""
    for f in facets or []:
        name = str(f.get("facetParameter") or "")
        if "location" not in name.lower():
            continue
        for v in f.get("values") or []:
            if region_re.search(str(v.get("descriptor") or "")) and v.get("id"):
                return {name: [str(v["id"])]}
    return {}


def to_rawjob(entry, base: str, site: str, p: Dict[str, Any], info: Dict[str, Any]) -> RawJob:
    tenant = entry.params["tenant"]
    bullets = [b for b in (p.get("bulletFields") or []) if b]
    req_id = info.get("jobReqId") or (bullets[0] if bullets else "") or p.get("externalPath", "")
    country = info.get("country") or {}
    return RawJob(
        platform_id="workday:%s:%s" % (tenant, req_id),
        title=info.get("title") or p.get("title", ""),
        company=entry.company,
        url=info.get("externalUrl") or "%s/%s%s" % (base, site, p.get("externalPath", "")),
        jd_text=html_to_text(info.get("jobDescription") or ""),
        location=info.get("location") or p.get("locationsText", ""),
        salary_raw="",
        posted_at=info.get("startDate") or "",
        raw={"posted_on": p.get("postedOn", ""), "time_type": info.get("timeType", ""), "job_req_id": req_id,
             "country": country.get("descriptor", "") if isinstance(country, dict) else "",
             "external_path": p.get("externalPath", "")},
    )


@register("workday")
def fetch_company(entry, http, limits: Dict[str, Any], limiter: RateLimiter, log: Callable[[str], None]) -> List[RawJob]:
    """Workday CXS：POST …/wday/cxs/<tenant>/<site>/jobs 翻页；有地区 facet 就用 facet，否则逐页按
    locationsText 过滤（不能等整个循环翻完再过滤——没有地区 facet 的租户，命中的岗位可能只落在某一页，
    该页命中数与总条数无关，不能拿"这一页 0 命中"或"已攒够的未过滤条数"当停止信号）；再逐条 GET 详情。"""
    from jp.adapters.watchlist import REGION_RE
    p = entry.params
    tenant, site, wd = p["tenant"], p["site"], p.get("wd", "wd3")
    base = "https://%s.%s.myworkdayjobs.com" % (tenant, wd)
    api = "%s/wday/cxs/%s/%s" % (base, tenant, site)
    search_text = str(p.get("search_text", "intern"))
    max_pages, detail_limit = limit(entry, limits, "max_pages", 3), limit(entry, limits, "detail_limit", 30)
    region_re = REGION_RE[entry.region]

    def page(offset: int, facets: Dict[str, List[str]]) -> Dict[str, Any]:
        limiter.wait()
        return http.post_json(api + "/jobs", {"appliedFacets": facets, "limit": PAGE, "offset": offset, "searchText": search_text}) or {}

    first = page(0, {})
    facets = _region_facet(first.get("facets") or [], region_re)
    current = page(0, facets) if facets else first
    postings: List[Dict[str, Any]] = []
    seen_paths = set()

    def add(batch: List[Dict[str, Any]]) -> Tuple[int, int]:
        """batch（这一页原始 jobPostings） → (raw_count, kept_count)。没有地区 facet 时逐条按
        locationsText 过滤，再去重累加；kept_count 只统计过滤 + 去重后真正新增的条数。"""
        fresh = 0
        for x in batch:
            if not facets and not region_re.search(str(x.get("locationsText") or "")):
                continue
            key = x.get("externalPath") or x.get("title")
            if key and key not in seen_paths:
                seen_paths.add(key)
                postings.append(x)
                fresh += 1
        return len(batch), fresh

    add(current.get("jobPostings") or [])
    total = int(current.get("total") or 0)
    for n in range(1, max_pages):
        if len(postings) >= detail_limit:
            break
        if total and (n * PAGE) >= total:   # offset 已经超过站方给的总数，翻下去也是空页
            break
        raw, kept = add(page(n * PAGE, facets).get("jobPostings") or [])
        if raw == 0:   # 整页原始记录都是空的才停；某一页 0 命中（kept==0）不能当停止信号
            break
    log("workday %s：候选 %d 条（facet=%s）" % (entry.company, len(postings), facets or "无，逐页按地点过滤"))
    out: List[RawJob] = []
    for x in postings[:detail_limit]:
        limiter.wait()
        info = (http.get_json(api + x.get("externalPath", "")) or {}).get("jobPostingInfo") or {}
        out.append(to_rawjob(entry, base, site, x, info))
    return out
