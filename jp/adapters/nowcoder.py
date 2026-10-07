from __future__ import annotations
import json
from typing import Any, Callable, Dict, List

from jp.adapters.base import Adapter, AdapterError, AuthRequiredError, RateLimiter, SearchQuery, ms_to_date
from jp.adapters.opencli import browser_close, browser_eval, run_opencli
from jp.models import RawJob

SESSION = "jp-nowcoder"
ENTRY_URL = "https://www.nowcoder.com/jobs/intern/center"
DETAIL_URL = "https://www.nowcoder.com/jobs/detail/%s"

# 在页面上下文里发 XHR（带站点 cookie）。不含 '%'：URL 编码交给 URLSearchParams。
_JS = (
    "(()=>new Promise((res,rej)=>{const x=new XMLHttpRequest();"
    "x.open('POST','https://www.nowcoder.com/np-api/u/job/square-search?_='+Date.now());"
    "x.setRequestHeader('Content-Type','application/x-www-form-urlencoded; charset=UTF-8');"
    "x.setRequestHeader('X-Requested-With','XMLHttpRequest');x.withCredentials=true;"
    "x.onloadend=()=>res(x.responseText);x.onerror=()=>rej(new Error('xhr error'));"
    "x.send(new URLSearchParams(__BODY__).toString())}))()"
)


RECRUIT_TYPES = {1: "校招", 2: "实习"}     # square-search 的 recruitType（1 校招 / 2 实习，2026-09-22 真机核实）


def build_js(kw: str, city: str, page: int, page_size: int, recruit_type: str = "2") -> str:
    body = {"requestFrom": "1", "page": str(page), "pageSize": str(page_size), "recruitType": str(recruit_type or "2"),
            "pageSource": "5001", "query": kw, "jobCity": city}
    return _JS.replace("__BODY__", json.dumps(body, ensure_ascii=False))


def to_rawjob(d: Dict[str, Any]) -> RawJob:
    """square-search 的一条 datas[].data → RawJob（列表自带 JD，不用抓详情）。"""
    try:
        ext = json.loads(d.get("ext") or "{}")
        if not isinstance(ext, dict):
            ext = {}
    except (ValueError, TypeError):   # TypeError：ext 本身已经不是字符串（比如站方直接给了个 dict）
        ext = {}
    comp = d.get("recommendInternCompany") or {}
    lines: List[str] = []
    if ext.get("infos"):
        lines.append("岗位职责：\n%s" % ext["infos"])
    if ext.get("requirements"):
        lines.append("任职要求：\n%s" % ext["requirements"])
    cond: List[str] = []
    if d.get("durationDays"):
        cond.append("每周 %s 天" % d["durationDays"])
    if d.get("durationMonths"):
        cond.append("至少 %s 个月" % d["durationMonths"])
    if d.get("jobOffer") == 1:
        cond.append("有转正机会")
    if cond:
        lines.append("到岗要求：%s" % "，".join(cond))
    salary = ""
    if d.get("salaryMin") or d.get("salaryMax"):
        salary = "%s-%s%s" % (d.get("salaryMin"), d.get("salaryMax"), "元/天" if d.get("salaryType") == 1 else "")
    boss = d.get("apiSimpleBossUser") or {}
    return RawJob(
        platform_id=str(d.get("id", "")),
        title=d.get("jobName", ""),
        company=comp.get("companyName") or comp.get("companyShortName") or "(待抽取)",
        url=DETAIL_URL % d.get("id", ""),
        jd_text="\n".join(lines).strip(),
        location=d.get("jobCity") or "，".join(d.get("jobCityList") or []),
        salary_raw=salary,
        posted_at=ms_to_date(d.get("refreshTime")),
        raw={"days_per_week": d.get("durationDays"), "min_months": d.get("durationMonths"), "job_offer": d.get("jobOffer"),
             "recruit_type": RECRUIT_TYPES.get(d.get("recruitType"), ""), "graduation_year": d.get("graduationYear") or "",
             "edu_level": d.get("eduLevel"), "job_keys": d.get("jobKeys"), "redirect_external_url": d.get("redirectExternalUrl"),
             "industry": d.get("industryName"), "company_scale": comp.get("personScales"),
             "boss_process_rate": boss.get("avgProcessRate"), "deliver_end": d.get("deliverEnd")},
    )


class NowcoderAdapter(Adapter):
    """牛客实习职位：先 `nowcoder whoami` 验登录，再在页面上下文里翻页调 square-search。只搜不投。"""
    source = "nowcoder"
    region = "CN"

    def __init__(self, run: Callable[..., Any] = run_opencli, evaluate: Callable[..., str] = browser_eval,
                 close: Callable[..., None] = browser_close):
        self.run, self.evaluate, self.close = run, evaluate, close

    def search(self, q: SearchQuery, limiter: RateLimiter, log: Callable[[str], None] = print) -> List[RawJob]:
        self.run(["nowcoder", "whoami", "-f", "json", "--window", "background"])   # 未登录在这里抛 AuthRequiredError
        seen: Dict[str, RawJob] = {}
        opened = False
        try:
            for kw in q.keywords:
                for page in range(1, q.max_pages + 1):
                    limiter.wait()
                    text = self.evaluate(SESSION, None if opened else ENTRY_URL,
                                         build_js(kw, q.city or "深圳", page, q.page_size, str(q.extra.get("recruit_type") or "2")))
                    opened = True
                    try:
                        resp = json.loads(text)
                    except ValueError:
                        raise AdapterError("nowcoder square-search 非 JSON: %s" % text[:120])
                    if resp.get("code") != 0:
                        msg = str(resp.get("msg", ""))
                        if "登录" in msg:
                            raise AuthRequiredError("nowcoder: %s" % msg)
                        raise AdapterError("nowcoder square-search code=%s %s" % (resp.get("code"), msg))
                    data = resp.get("data") or {}
                    recs = [r.get("data") or {} for r in (data.get("datas") or [])]
                    fresh = 0
                    for d in recs:
                        if d.get("id") and str(d["id"]) not in seen:
                            seen[str(d["id"])] = to_rawjob(d)
                            fresh += 1
                    log("nowcoder %r 第 %d 页：%d 条（新 %d）" % (kw, page, len(recs), fresh))
                    if not recs or fresh == 0 or page >= int(data.get("totalPage") or page):
                        break
        finally:
            if opened:
                self.close(SESSION)
        return list(seen.values())
