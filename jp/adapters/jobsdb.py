from __future__ import annotations
import json
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

from jp.adapters.base import Adapter, AdapterError, RateLimiter, RiskControlError, SearchQuery, html_to_text, split_for_detail
from jp.http import HttpClient
from jp.models import RawJob

SEARCH_URL = "https://hk.jobsdb.com/api/jobsearch/v5/search"
JOB_URL = "https://hk.jobsdb.com/job/%s"


def _label(obj: Optional[Dict[str, Any]]) -> str:
    """SEEK Apollo 缓存里的字段名带参数，如 'label({"locale":"en-HK"})' / 'name({"locale":"en-HK"})'。"""
    for k, v in (obj or {}).items():
        if (k.startswith("label(") or k.startswith("name(")) and isinstance(v, str):
            return v
    return ""


def parse_detail(html: str) -> Dict[str, Any]:
    """详情页 window.SEEK_APOLLO_DATA → {description, is_link_out, expires_at, posted_at, work_type, location, advertiser}；结构变了返回 {}。"""
    i = html.find("window.SEEK_APOLLO_DATA")
    if i < 0:
        return {}
    j = html.find("{", i)
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[j:])
    except ValueError:
        return {}
    job = None
    for k, v in (obj.get("ROOT_QUERY") or {}).items():
        if k.startswith("jobDetails:") and isinstance(v, dict):
            job = v.get("job")
            break
    if not isinstance(job, dict):
        return {}
    content = ""
    for k, v in job.items():
        if k.startswith("content2(") and isinstance(v, str):
            content = v
    return {
        "description": html_to_text(content),
        "is_link_out": job.get("isLinkOut"),
        "expires_at": ((job.get("expiresAt") or {}).get("dateTimeUtc") or "")[:10],
        "posted_at": ((job.get("listedAt") or {}).get("dateTimeUtc") or "")[:10],
        "work_type": _label(job.get("workTypes")),
        "location": _label(job.get("location")),
        "advertiser": _label(job.get("advertiser")),
    }


def to_rawjob(row: Dict[str, Any], detail: Dict[str, Any]) -> RawJob:
    pid = str(row.get("id", ""))
    locs = row.get("locations") or []
    arrangement = (row.get("workArrangements") or {}).get("displayText") or ""
    if not arrangement:
        data = (row.get("workArrangements") or {}).get("data") or []
        arrangement = ((data[0].get("label") or {}).get("text") if data else "") or ""
    fallback = "\n".join([row.get("teaser") or ""] + [b for b in (row.get("bulletPoints") or []) if b]).strip()
    return RawJob(
        platform_id=pid,
        title=row.get("title", ""),
        company=row.get("companyName") or (row.get("advertiser") or {}).get("description") or "(待抽取)",
        url=JOB_URL % pid,
        jd_text=detail.get("description") or fallback,
        location=detail.get("location") or (locs[0].get("label", "") if locs else ""),
        salary_raw=row.get("salaryLabel") or "",
        posted_at=(row.get("listingDate") or detail.get("posted_at") or "")[:10],
        raw={"work_types": row.get("workTypes") or [], "work_arrangement": arrangement,
             "is_link_out": detail.get("is_link_out"), "expires_at": detail.get("expires_at", ""),
             "classifications": row.get("classifications") or [], "advertiser_id": (row.get("advertiser") or {}).get("id", "")},
    )


class JobsdbAdapter(Adapter):
    """JobsDB（SEEK 平台）：公开搜索接口翻页 → 逐条详情页解析 Apollo 数据。不需要浏览器与登录。"""
    source = "jobsdb"
    region = "HK"

    def __init__(self, http: Any = None):
        self.http = http or HttpClient()

    def search(self, q: SearchQuery, limiter: RateLimiter, log: Callable[[str], None] = print) -> List[RawJob]:
        rows: Dict[str, Dict[str, Any]] = {}
        for kw in q.keywords:
            for page in range(1, q.max_pages + 1):
                limiter.wait()
                params = urllib.parse.urlencode({"siteKey": "HK-Main", "sourcesystem": "houston", "keywords": kw,
                                                 "page": page, "pageSize": q.page_size, "locale": "en-HK", "sortmode": "ListedDate"})
                resp = self.http.get_json(SEARCH_URL + "?" + params)
                batch = resp.get("data") or []
                fresh = 0
                for r in batch:
                    pid = str(r.get("id", ""))
                    if pid and pid not in rows:
                        rows[pid] = r
                        fresh += 1
                log("jobsdb %r 第 %d 页：%d 条（新 %d，总 %s）" % (kw, page, len(batch), fresh, resp.get("totalCount")))
                if not batch or fresh == 0 or len(batch) < q.page_size:
                    break
        out: List[RawJob] = []
        fetch, known, over = split_for_detail(rows, q.detail_limit, q.extra.get("known_ids"))
        for pid, r in known:                    # 已入库：不再请求详情页，回列表行让 ingest 记"已见"
            rj = to_rawjob(r, {})
            rj.raw["detail_skipped"] = "known"
            out.append(rj)
        failed = 0
        for pid, r in fetch:
            limiter.wait()
            try:
                html = self.http.get_text(JOB_URL % pid)
            except RiskControlError:
                raise
            except AdapterError as e:            # 单条详情页 404 / 5xx：回退列表字段（teaser + bulletPoints），不拖垮整个来源
                log("jobsdb 详情失败，退回列表字段: %s（%s）" % (pid, str(e)[:120]))
                rj = to_rawjob(r, {})
                rj.raw["detail_skipped"] = "error"
                out.append(rj)
                failed += 1
                continue
            out.append(to_rawjob(r, parse_detail(html)))
        log("jobsdb 详情 %d 条（失败退回 %d、已入库跳过 %d、超预算留待下次 %d）" % (len(fetch) - failed, failed, len(known), over))
        return out
