from __future__ import annotations
import json
import re
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode

from jp.adapters.base import (Adapter, AdapterError, AuthRequiredError, RateLimiter, RiskControlError,
                              SearchQuery, looks_like_risk)
from jp.adapters.opencli import browser_close, browser_eval, daemon_restart, run_opencli
from jp.models import RawJob

SESSION = "jp-boss"
ENTRY_URL = "https://www.zhipin.com/web/geek/jobs?"
LIST_URL = "https://www.zhipin.com/wapi/zpgeek/search/joblist.json"
JOB_URL = "https://www.zhipin.com/job_detail/%s.html"
PAGE_SIZE = 15
CITY_CODES = {"深圳": "101280600", "香港": "101320100", "北京": "101010100", "上海": "101020100", "广州": "101280100",
              "杭州": "101210100", "东莞": "101281600", "珠海": "101280700", "成都": "101270100"}
EXP_CODES = {"在校生": "108", "在校": "108", "应届": "108", "在校/应届": "108", "应届生": "102", "经验不限": "101", "不限": "0"}
JOB_TYPE_CODES = {"实习": "1902", "全职": "1901", "兼职": "1903", "不限": "0"}
# 37（"您的环境存在异常."）不是登录态问题：__zp_stoken__ 令牌过期，重新打开入口页刷新一次即可恢复，
# 见 _call。真正的 cookie 过期码只剩 1 / 3 / 7（官方 COOKIE_EXPIRED_CODES 去掉 37）。
AUTH_CODES = {1, 3, 7}
MAX_TOKEN_REFRESHES = 3
PAGE_READY_TRIES = 3          # 入口页最多确认几次（每次之间等一个限速间隔）
DAEMON_RESTART_WAIT = 12.0    # 重启守护进程后等扩展重新连上的秒数
DETAIL_RENDER_WAIT = 2.5     # 详情页首读为空时等待多少秒再读一次（页面 JS 渲染）
ZHIPIN_ORIGIN = "https://www.zhipin.com/"
_HREF_JS = "location.href"   # 不含 '%'，单独一次极小的 eval，跟业务 XHR 分开

_DAYS_RE = re.compile(r"(\d)\s*天\s*/\s*周")
_MONTHS_RE = re.compile(r"(\d+)\s*个月")
_ID_RE = re.compile(r"/job_detail/([A-Za-z0-9_\-~]+)\.html")
_EXPERIENCE_FRAGMENT_RE = re.compile(r"(\d\s*天\s*/\s*周(?:\s*\d+\s*个月)?)")
_DEGREE_RE = re.compile(r"博士|硕士|本科|大专|学历不限")
_COMPANY_TITLE_RE = re.compile(r"_(.+?)招聘-BOSS直聘")

# 页面上下文里的 XHR；不含 '%'（Windows 下 opencli 是 npm 的 .cmd 垫片，经 cmd.exe 时 '%x%' 会被当变量展开），
# URL 编码交给页面里的 URLSearchParams。只用于列表 joblist.json——detail.json 这条裸接口被站方按次数限流
# （短时间内连续几次直接调用就报 code 37），改成像人一样点开岗位详情页读 DOM，见 DETAIL_DOM_JS。
_JS = ("(()=>new Promise((res,rej)=>{const x=new XMLHttpRequest();"
       "x.open('GET',__URL__+'?'+new URLSearchParams(__Q__).toString(),true);x.withCredentials=true;"
       "x.setRequestHeader('Accept','application/json');"
       "x.onload=()=>res(x.responseText);x.onerror=()=>rej(new Error('xhr error'));x.send(null)}))()")

# 岗位详情页 DOM 提取；不含 '%'。text(x)：x 是选择器字符串就先 querySelector，是元素/null 就直接用；
# 取 innerText.trim()，找不到返回 ''。同步表达式（不是 Promise），因为这里只是读已经渲染好的页面。
DETAIL_DOM_JS = (
    "(()=>{const text=x=>{const el=typeof x==='string'?document.querySelector(x):x;"
    "return el?(el.innerText||'').trim():'';};"
    "return JSON.stringify({url:location.href.split('?')[0],title:document.title,"
    "name:text('h1'),salary:text('.salary'),"
    "primary:text('.job-primary')||text('.info-primary'),"
    "sections:[...document.querySelectorAll('.job-sec, .job-detail-section')]"
    ".map(s=>({h:text(s.querySelector('h3')).slice(0,20),text:text(s.querySelector('.job-sec-text'))})),"
    "address:text('.location-address'),"
    "welfare:[...document.querySelectorAll('.job-tags span, .tag-all span')].map(e=>e.innerText.trim())"
    "});})()"
)


def build_js(url: str, params: Dict[str, str]) -> str:
    return _JS.replace("__URL__", json.dumps(url)).replace("__Q__", json.dumps(params, ensure_ascii=False))


def city_code(city: str) -> str:
    """纯数字（调用方已经传城市码）原样返回；表里有的城市名映射成码；其它城市名原样交给站方接口。"""
    c = (city or "").strip()
    if c.isdigit():
        return c
    return CITY_CODES.get(c, c)


def parse_experience(text: str) -> Dict[str, Optional[int]]:
    """Boss 实习岗 experience 文本形如 '4天/周 6个月'（真机核实）→ 到岗天数 / 最短月数；缺失为 None。"""
    d = _DAYS_RE.search(text or "")
    m = _MONTHS_RE.search(text or "")
    return {"days_per_week": int(d.group(1)) if d else None, "min_months": int(m.group(1)) if m else None}


def _clean_company(name: str) -> str:
    """站方把品牌名截断成 'xx...' 时视为缺失，让 analyze 从 JD 回填。"""
    n = (name or "").strip()
    if not n or n.endswith("...") or n.endswith("…"):
        return "(待抽取)"
    return n


def _dedup(items: List[str]) -> List[str]:
    seen, out = set(), []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _section_text(sections: List[Dict[str, Any]], prefix: str) -> str:
    for sec in sections:
        if (sec.get("h") or "").startswith(prefix):
            return sec.get("text") or ""
    return ""


def list_row(j: Dict[str, Any]) -> Dict[str, Any]:
    """joblist.json 的一条 → 与旧官方适配器同形的列表行（to_rawjob 的 row）。"""
    return {"name": j.get("jobName", ""), "salary": j.get("salaryDesc", ""), "company": j.get("brandName", ""),
            "area": "·".join(x for x in (j.get("cityName"), j.get("areaDistrict"), j.get("businessDistrict")) if x),
            "experience": " ".join(x for x in (j.get("daysPerWeekDesc"), j.get("leastMonthDesc")) if x) or j.get("jobExperience", ""),
            "degree": j.get("jobDegree", ""), "skills": ", ".join(j.get("skills") or []),
            "boss": " · ".join(x for x in (j.get("bossName"), j.get("bossTitle")) if x),
            "bossOnline": "Y" if j.get("bossOnline") else "N", "security_id": j.get("securityId", ""),
            "url": JOB_URL % j.get("encryptJobId", "") if j.get("encryptJobId") else "",
            "industry": j.get("brandIndustry", ""), "scale": j.get("brandScaleName", ""), "stage": j.get("brandStageName", "")}


def detail_from_dom(d: Dict[str, Any]) -> Dict[str, Any]:
    """岗位详情页 DOM 提取结果（DETAIL_DOM_JS 的输出）→ 与旧 detail.json 同形的详情字典（to_rawjob 的
    detail）。boss_name/boss_title/active_time/skills/industry/scale/stage 这些原来 detail.json 独有、
    页面 DOM 里没有的字段统一置空，由 to_rawjob 退回列表行 row 的对应字段兜底。"""
    primary = d.get("primary") or ""
    title = d.get("title") or ""
    sections = d.get("sections") or []

    exp_m = _EXPERIENCE_FRAGMENT_RE.search(primary)
    experience = exp_m.group(1) if exp_m else ""

    degree_m = _DEGREE_RE.search(primary)
    degree = degree_m.group(0) if degree_m else ""

    city = ""
    for line in primary.splitlines():
        if _DAYS_RE.search(line):
            parts = line.split()
            if parts:
                city = parts[0]
            break

    description = _section_text(sections, "职位描述")
    if not description and sections:
        description = max((sec.get("text") or "" for sec in sections), key=len)

    company_m = _COMPANY_TITLE_RE.search(title)
    company = company_m.group(1) if company_m else ""

    return {"name": d.get("name", ""), "salary": d.get("salary", ""), "experience": experience,
            "degree": degree, "city": city, "district": "",
            "description": description, "skills": "",
            "welfare": ", ".join(_dedup(d.get("welfare") or [])),
            "boss_name": "", "boss_title": "", "active_time": "",
            "company": company, "company_intro": _section_text(sections, "公司介绍"),
            "industry": "", "scale": "", "stage": "",
            "address": d.get("address", ""), "url": d.get("url", "")}


def _skip_detail_reason(row: Dict[str, Any], extra: Dict[str, Any]) -> Optional[str]:
    """列表行是否值得花一次详情导航：已入库的（extra["known_ids"]）不抓；列表自带的到岗天数 / 最短月数已超出
    硬条件上限（extra["max_days_per_week"] / ["max_min_months"]，由 fetch 从 constraints.yaml 传入）的也不抓——
    这两类仍会以列表字段返回，前者让 ingest 记"已见"，后者让 prescore 记下硬伤原因。"""
    pid_m = _ID_RE.search(row.get("url") or "")
    pid = pid_m.group(1) if pid_m else ""
    if pid and pid in (extra.get("known_ids") or ()):
        return "known"
    exp = parse_experience(row.get("experience") or "")
    max_d, max_m = extra.get("max_days_per_week"), extra.get("max_min_months")
    if max_d is not None and exp["days_per_week"] is not None and exp["days_per_week"] > int(max_d):
        return "hard_prefilter"
    if max_m is not None and exp["min_months"] is not None and exp["min_months"] > int(max_m):
        return "hard_prefilter"
    return None


def to_rawjob(row: Dict[str, Any], detail: Dict[str, Any]) -> RawJob:
    """列表行 + 详情 → RawJob。公司名以详情为准，缺失或被站方截断（品牌名 "xx..."）统一置 "(待抽取)"，留给
    analyze 从 JD 回填；到岗天数/时长优先解析详情的 experience（"N天/周 M个月"），详情没有则退回列表自带的
    daysPerWeekDesc/leastMonthDesc 拼成的文本；industry/scale 同理——DOM 详情页没有这两个字段，退回列表。"""
    url = row.get("url") or detail.get("url") or ""
    m = _ID_RE.search(url)
    exp_text = detail.get("experience") or row.get("experience") or ""
    exp = parse_experience(exp_text)
    lines: List[str] = []
    if detail.get("description"):
        lines.append(detail["description"])
    if detail.get("skills"):
        lines.append("技能标签：%s" % detail["skills"])
    if detail.get("welfare"):
        lines.append("福利：%s" % detail["welfare"])
    if exp_text:
        lines.append("到岗要求：%s" % exp_text)
    location = "·".join(x for x in (detail.get("city"), detail.get("district")) if x) or row.get("area", "")
    company = _clean_company(detail.get("company") or row.get("company") or "")
    return RawJob(
        platform_id=m.group(1) if m else (row.get("security_id") or "")[:40],
        title=detail.get("name") or row.get("name", ""),
        company=company,
        url=url,
        jd_text="\n".join(lines).strip(),
        location=location,
        salary_raw=detail.get("salary") or row.get("salary", ""),
        posted_at="",
        raw={"list": row, "detail": detail,
             "days_per_week": exp["days_per_week"], "min_months": exp["min_months"],
             "security_id": row.get("security_id", ""), "boss_active": detail.get("active_time", ""),
             "degree": detail.get("degree") or row.get("degree", ""),
             "industry": detail.get("industry") or row.get("industry", ""),
             "scale": detail.get("scale") or row.get("scale", ""),
             "company_intro": detail.get("company_intro", "")},
    )


class BossAdapter(Adapter):
    """Boss 直聘：官方 `boss search/detail` 因站点路由跳转（/web/geek/job → /web/geek/jobs）导致 OpenCLI 页面标识
    失效（stale page identity）不可用；改为自建浏览器会话，`whoami` 验登录后在页面上下文里 XHR 站内接口
    `joblist.json` 搜列表（与牛客适配器同一模式）。详情不再调裸 `detail.json`——那条接口对短时间内的连续直调
    按次数限流（code 37），改成像人一样导航到岗位详情页 `job_detail/<id>.html` 读 DOM。"""
    source = "boss"
    region = "CN"

    def __init__(self, run: Callable[..., Any] = run_opencli, evaluate: Callable[..., str] = browser_eval,
                 close: Callable[..., None] = browser_close, sleep: Callable[[float], None] = time.sleep,
                 restart_daemon: Callable[..., bool] = daemon_restart):
        self.run, self.evaluate, self.close, self._sleep = run, evaluate, close, sleep
        self.restart_daemon = restart_daemon
        self._refreshes = 0     # search() 每次开搜都会重置；跨 _call 累计，防止"刷新后又刷新"反复重试
        self._limiter: Optional[RateLimiter] = None   # search() 期间指向调用方传入的 limiter
        self._log: Callable[[str], None] = print       # search() 期间指向调用方传入的 log

    def _eval_json(self, url: str, params: Dict[str, str], open_url: Optional[str]) -> Dict[str, Any]:
        text = self.evaluate(SESSION, open_url, build_js(url, params))
        try:
            return json.loads(text)
        except ValueError:
            raise AdapterError("boss 接口非 JSON: %s" % text[:200])

    def _location_href(self, open_url: Optional[str]) -> str:
        text = self.evaluate(SESSION, open_url, _HREF_JS)
        return (text or "").strip()

    def _ensure_page_ready(self, entry_url: str) -> None:
        """search() 打开会话后只做一次：真机观测到共享的 "OpenCLI Browser" 标签页偶发 open 之后仍
        停在 about:blank（另一个命令刚好还没跳转完，或守护进程换过一轮、会话租约已失效），此时同源
        XHR 会直接网络失败。这里主动确认 location.href 落在 zhipin.com 上；重开几次都还是 about:blank
        就重启一次守护进程换新租约（2026-09-23 真机：整夜连抓时第二个查询块必然卡在 about:blank，
        close + 重开无效，只有 daemon restart 能恢复），再不行才放弃。"""
        for attempt in range(PAGE_READY_TRIES):
            href = self._location_href(entry_url)
            if href.startswith(ZHIPIN_ORIGIN):
                return
            if attempt + 1 == PAGE_READY_TRIES:
                break
            self._limiter.wait()
        self._log("boss 会话卡在 %s，重启 opencli 守护进程换新租约" % (href or "空页面"))
        self.restart_daemon()
        self._sleep(DAEMON_RESTART_WAIT)
        href = self._location_href(entry_url)
        if not href.startswith(ZHIPIN_ORIGIN):
            raise AdapterError("boss 页面未就绪: %s" % href)

    def _refresh_and_retry(self, url: str, params: Dict[str, str], entry_url: str) -> Dict[str, Any]:
        """令牌过期（code 37）与 XHR 网络失败（同一共享标签页偶发还没导航到位）共用的恢复动作：计数、
        超过上限直接按风控冷却；否则等一下、用 entry_url 强制重新导航后重放同一个请求。"""
        self._refreshes += 1
        if self._refreshes > MAX_TOKEN_REFRESHES:
            raise RiskControlError("boss 环境异常连续出现，停止并冷却")
        self._limiter.wait()
        return self._eval_json(url, params, entry_url)

    def _call(self, url: str, params: Dict[str, str], entry_url: str) -> Dict[str, Any]:
        """列表 joblist.json 专用：evaluate → JSON → code 判定。会话已经在 search() 里打开并确认过落在
        zhipin.com，这里第一次永远传 open_url=None 复用它；两类瞬时异常共用 _refresh_and_retry 重新导航
        重试一次：
        - evaluate 本身抛 AdapterError 且 message 含 "xhr error"（网络层失败）→ 重试；仍失败就把原异常原样抛出（不算风控）。
        - code==37（令牌过期）→ 重试；仍是 37 才当风控。
        重试拿到新响应后落回正常分类：0 正常；风控字样→RiskControlError；AUTH_CODES/"登录"→AuthRequiredError；否则 AdapterError。"""
        try:
            data = self._eval_json(url, params, None)
        except AdapterError as e:
            if "xhr error" not in str(e):
                raise
            data = self._refresh_and_retry(url, params, entry_url)   # 若重试仍是 xhr error，异常原样往外传
        code = data.get("code")
        if code == 37:
            data = self._refresh_and_retry(url, params, entry_url)
            code = data.get("code")
            if code == 37:
                raise RiskControlError("boss 环境异常（code 37），重导航后仍异常")
        if code == 0:
            self._refreshes = 0   # 拿到成功响应就清零：预算按"连续"刷新计，不是整次 search() 累计
            return data
        message = str(data.get("message", ""))
        if looks_like_risk(message):
            raise RiskControlError("boss 接口疑似风控: %s" % message)
        if code in AUTH_CODES or "登录" in message:
            raise AuthRequiredError("boss 接口: %s" % message)
        raise AdapterError("boss 接口 code=%s %s" % (code, message))

    def _fetch_detail(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """导航到岗位详情页读 DOM（wapi 的 detail.json 接口对短时间内连续直调按次数限流到 code 37；
        像人一样点开真实页面不受影响）。单条页面解析失败不能打断整次抓取，退回空 detail——to_rawjob
        仍能用列表行 row 兜底出 title/company/salary/area/experience。"""
        d = self._read_detail_dom(row["url"])
        if d is None:
            self._sleep(DETAIL_RENDER_WAIT)      # 页面 JS 通常还没渲染完：等一下在同一页面上再读一次，不重新导航
            d = self._read_detail_dom(None)
        if d is None:
            self._log("boss 详情页解析失败，退回列表字段: %s" % row.get("url", ""))
            return {}
        if looks_like_risk(d.get("title", "")) or looks_like_risk(d.get("primary", "")):
            raise RiskControlError("boss 详情页疑似风控: %s" % (d.get("title") or d.get("primary") or "")[:120])
        return detail_from_dom(d)

    def _read_detail_dom(self, url: Optional[str]) -> Optional[Dict[str, Any]]:
        """读一次详情页 DOM；拿不到岗位名和正文段落（页面未渲染 / 结构变了 / 非 JSON）返回 None。"""
        text = self.evaluate(SESSION, url, DETAIL_DOM_JS)
        try:
            d = json.loads(text)
        except ValueError:
            return None
        if not isinstance(d, dict) or (not d.get("name") and not d.get("sections")):
            return None
        return d

    def search(self, q: SearchQuery, limiter: RateLimiter, log: Callable[[str], None] = print) -> List[RawJob]:
        self.run(["boss", "whoami", "-f", "json", "--window", "background"])   # 未登录在这里抛 AuthRequiredError
        self._limiter = limiter
        self._log = log
        self._refreshes = 0
        city = city_code(q.city or "深圳")
        exp_code = EXP_CODES.get(q.extra["experience"], q.extra["experience"]) if q.extra.get("experience") else ""
        job_type_code = JOB_TYPE_CODES.get(q.extra["job_type"], q.extra["job_type"]) if q.extra.get("job_type") else ""
        rows: Dict[str, Dict[str, Any]] = {}
        opened = False
        entry_url = ""
        try:
            for kw in q.keywords:
                qs = {"query": kw, "city": city}
                if exp_code:
                    qs["experience"] = exp_code
                if job_type_code:
                    qs["jobType"] = job_type_code
                entry_url = ENTRY_URL + urlencode(qs)   # 与 XHR 同样的过滤条件，令牌刷新/重开导航到位就是这个页面
                if not opened:
                    opened = True   # 从这一刻起会话已经/正在被打开，无论下面这步是否成功都要在 finally 里关掉
                    self._ensure_page_ready(entry_url)
                for page in range(1, q.max_pages + 1):
                    limiter.wait()
                    params = {"scene": "1", "query": kw, "city": city, "page": str(page),
                              "pageSize": str(q.page_size or PAGE_SIZE)}
                    if exp_code:
                        params["experience"] = exp_code
                    if job_type_code:
                        params["jobType"] = job_type_code
                    data = self._call(LIST_URL, params, entry_url)
                    zp = data.get("zpData") or {}
                    batch = zp.get("jobList") or []
                    fresh = 0
                    for j in batch:
                        key = j.get("securityId") or j.get("encryptJobId")
                        if key and key not in rows:
                            rows[key] = list_row(j)
                            fresh += 1
                    log("boss search %r 第 %d 页：%d 条（新 %d）" % (kw, page, len(batch), fresh))
                    if not batch or fresh == 0 or not zp.get("hasMore"):
                        break
            out: List[RawJob] = []
            budget = q.detail_limit
            skipped = {"hard_prefilter": 0, "known": 0, "over_limit": 0}
            for r in rows.values():
                skip = _skip_detail_reason(r, q.extra)
                if skip is None and budget <= 0:
                    skipped["over_limit"] += 1      # 详情预算用完的新岗位不返回，留给下次 fetch（那时它仍是未知的）
                    continue
                if skip is not None or not r.get("url"):
                    rj = to_rawjob(r, {})
                    if skip is not None:
                        rj.raw["detail_skipped"] = skip
                        skipped[skip] += 1
                    out.append(rj)
                    continue
                budget -= 1
                limiter.wait()
                detail = self._fetch_detail(r)
                rj = to_rawjob(r, detail)
                if not detail:
                    rj.raw["detail_skipped"] = "error"     # 让 ingest / known_ids 知道它只有列表字段，下次抓取再补
                out.append(rj)
            log("boss 详情 %d 条（列表 %d 条：硬条件预筛跳过 %d、已入库 %d、超预算留待下次 %d）"
                % (q.detail_limit - budget, len(rows), skipped["hard_prefilter"], skipped["known"], skipped["over_limit"]))
            return out
        finally:
            if opened:
                self.close(SESSION)
