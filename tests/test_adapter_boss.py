from __future__ import annotations
import json
import pathlib
import pytest
from jp.adapters.base import AdapterError, AuthRequiredError, RateLimiter, RiskControlError, SearchQuery
from jp.adapters.boss import (BossAdapter, DETAIL_DOM_JS, _clean_company, city_code, detail_from_dom,
                              parse_experience)

FIX = pathlib.Path(__file__).resolve().parent / "fixtures" / "adapters"
JOBLIST = (FIX / "boss_joblist.json").read_text(encoding="utf-8")
DOM_FIXTURE = (FIX / "boss_detail_dom.json").read_text(encoding="utf-8")
EMPTY_LIST = json.dumps({"code": 0, "message": "Success", "zpData": {"hasMore": False, "jobList": []}})
ZHIPIN_HREF = "https://www.zhipin.com/web/geek/jobs?query=大模型 实习&city=101280600"
# 夹具 jobList 顺序：多模态岗位（首读为空，本轮靠列表字段兜底）在前，示例岗位（有合成 DOM 夹具）在后。
MULTIMODAL_JOB_URL = "https://www.zhipin.com/job_detail/FAKEBOSSJOBALPHA01.html"
DOUBAO_JOB_URL = "https://www.zhipin.com/job_detail/FAKEBOSSJOBALPHA02.html"


class Fake:
    def __init__(self, logged_in=True, code37_on_first_joblist=False,
                 xhr_error_on_first_joblist=False, blank_first_href=False):
        self.runs, self.evals, self.closed = [], [], []
        self.logged_in = logged_in
        self.code37_on_first_joblist = code37_on_first_joblist
        self.xhr_error_on_first_joblist = xhr_error_on_first_joblist
        self.blank_first_href = blank_first_href
        self.blank_hrefs = 0            # 前几次 location.href 都返回 about:blank（会话租约失效的真机现象）
        self.restarts = 0
        self._joblist_calls = 0
        self._href_calls = 0

    def run(self, args, **kw):
        self.runs.append(args)
        if not self.logged_in:
            raise AuthRequiredError("Boss 未登录")
        return {"logged_in": True, "site": "boss"}

    def evaluate(self, session, url, js, **kw):
        self.evals.append((session, url, js))
        if js == "location.href":
            self._href_calls += 1
            if self.blank_first_href and self._href_calls == 1:
                return "about:blank"
            if self._href_calls <= self.blank_hrefs:
                return "about:blank"
            return ZHIPIN_HREF
        if js == DETAIL_DOM_JS:
            # 详情不再靠 JS 内容区分，而是按打开的 url（岗位详情页）区分——示例岗位这条有合成 DOM 夹具，
            # 其它一律当成"页面渲染不出预期字段"，让 _fetch_detail 退回列表字段。
            return DOM_FIXTURE if url == DOUBAO_JOB_URL else "{}"
        if "joblist.json" in js:
            self._joblist_calls += 1
            if self.xhr_error_on_first_joblist and self._joblist_calls == 1:
                raise AdapterError("opencli browser eval 无输出: ✖  Error: xhr error")
            if self.code37_on_first_joblist and self._joblist_calls == 1:
                return json.dumps({"code": 37, "message": "您的环境存在异常."})
            return JOBLIST if '"page": "1"' in js else EMPTY_LIST
        raise AssertionError(js)

    def close(self, session, **kw):
        self.closed.append(session)


def _restarter(f):
    def restart(**kw):
        f.restarts += 1
        f.blank_hrefs = 0          # 重启守护进程换到新租约后页面就正常了
        return True
    return restart


def _adapter(f):
    return BossAdapter(run=f.run, evaluate=f.evaluate, close=f.close, sleep=lambda s: None,
                       restart_daemon=_restarter(f))


def _limiter():
    return RateLimiter(0, 0, sleep=lambda s: None)


def _query(**kw):
    base = dict(region="CN", keywords=["大模型 实习"], city="深圳",
                extra={"experience": "在校生", "job_type": "实习"})
    base.update(kw)
    return SearchQuery(**base)


def _fixed_json_response(payload):
    """打桩 evaluate：location.href 走正常地址，其它调用一律返回给定 JSON 文本——只关心 _call 对
    某个固定响应码的分类逻辑时用，不关心分页/详情/origin 检查细节（不会真的走到详情阶段）。"""
    def _fn(session, url, js, **kw):
        if js == "location.href":
            return ZHIPIN_HREF
        return payload
    return _fn


def test_city_code_has_chengdu():
    assert city_code("成都") == "101270100"


def test_city_code():
    assert city_code("深圳") == "101280600"
    assert city_code("101320100") == "101320100"
    assert city_code("武汉") == "武汉"


def test_parse_experience():
    assert parse_experience("4天/周 4个月") == {"days_per_week": 4, "min_months": 4}
    assert parse_experience("5天/周") == {"days_per_week": 5, "min_months": None}
    assert parse_experience("") == {"days_per_week": None, "min_months": None}


def test_clean_company():
    assert _clean_company("人工智能与数字经...") == "(待抽取)"
    assert _clean_company("星河科技") == "星河科技"


def test_detail_from_dom_parses_primary_and_title():
    d = {
        "title": "「某某实习生招聘」_某某科技招聘-BOSS直聘",
        "primary": "最新\n某某实习生 100-200元/天\n\n上海 5天/周 3个月 硕士\n\n感兴趣 立即沟通",
        "name": "某某实习生", "salary": "100-200元/天",
        "sections": [{"h": "职位描述", "text": "岗位职责：xxx"}, {"h": "公司介绍", "text": "公司简介：yyy"}],
        "address": "上海某地", "welfare": ["五险一金", "带薪年假", "五险一金"],
        "url": "https://www.zhipin.com/job_detail/abc.html",
    }
    detail = detail_from_dom(d)
    assert detail["experience"] == "5天/周 3个月"
    assert detail["degree"] == "硕士"
    assert detail["city"] == "上海"
    assert detail["company"] == "某某科技"
    assert detail["description"] == "岗位职责：xxx"
    assert detail["company_intro"] == "公司简介：yyy"
    assert detail["welfare"] == "五险一金, 带薪年假"   # 去重
    assert detail["industry"] == "" and detail["scale"] == "" and detail["skills"] == ""


def test_detail_from_dom_missing_fields_default_empty():
    d = {"title": "跟公司招聘格式对不上的标题", "primary": "", "name": "", "sections": [],
         "address": "", "welfare": [], "url": ""}
    detail = detail_from_dom(d)
    assert detail["description"] == ""
    assert detail["experience"] == "" and detail["degree"] == "" and detail["city"] == ""
    assert detail["company"] == ""


def test_detail_from_dom_description_falls_back_to_longest_section():
    d = {"title": "", "primary": "", "name": "",
         "sections": [{"h": "其它", "text": "短"}, {"h": "别的", "text": "这是比较长的一段介绍文字"}],
         "address": "", "welfare": [], "url": ""}
    detail = detail_from_dom(d)
    assert detail["description"] == "这是比较长的一段介绍文字"


def test_search_pages_and_maps_fields():
    f = Fake()
    lim = _limiter()
    jobs = _adapter(f).search(_query(), lim, log=lambda s: None)

    # whoami 先于任何 evaluate 调用
    assert f.runs[0][:2] == ["boss", "whoami"]

    # 会话打开后先做一次 location.href 校验，确认落在 zhipin.com 才继续；列表翻页复用同一会话（url=None）。
    assert f.evals[0][2] == "location.href"
    entry_url = f.evals[0][1]
    assert entry_url is not None and entry_url.startswith("https://www.zhipin.com/web/geek/jobs?")
    joblist_evals = [e for e in f.evals if "joblist.json" in e[2]]
    assert all(e[1] is None for e in joblist_evals)
    assert f.closed == ["jp-boss"]

    # build_js：无 '%'（Windows opencli .cmd 垫片经 cmd.exe 会展开 %var%），含站内接口与查询参数
    js0 = joblist_evals[0][2]
    assert "%" not in js0
    assert "joblist.json" in js0
    assert '"query": "大模型 实习"' in js0
    assert '"jobType": "1902"' in js0
    assert '"experience": "108"' in js0
    assert '"city": "101280600"' in js0

    # 详情改成导航到岗位详情页读 DOM：每条详情 eval 的 url 是各自的 job_detail 页面，不是 None。
    detail_evals = [e for e in f.evals if e[2] == DETAIL_DOM_JS]
    assert [e[1] for e in detail_evals] == [MULTIMODAL_JOB_URL, None, DOUBAO_JOB_URL]   # 多模态首读为空 → 同页重读一次

    # 分页：第 1 页 2 条且 hasMore=true → 继续翻页；第 2 页返回空 jobList → 停。
    # limiter.wait() 在每次外部调用前都会触发：1（第 1 页列表）+ 1（第 2 页列表，空）+ 2（详情）= 4；同页重读不占用 wait。
    # origin 校验首次成功不占用 wait。
    assert lim.calls == 4

    assert len(jobs) == 2
    a, b = jobs   # a=多模态（本轮 DOM 夹具里没有它，退回列表字段），b=示例岗位（有合成 DOM 夹具）

    assert a.platform_id == "FAKEBOSSJOBALPHA01"
    assert a.company == "(待抽取)"                    # row 的 brandName 也被截断成 "xx..."
    assert a.location == "深圳·光明区·光明"            # detail 为空，退回列表的 area
    assert a.salary_raw == "300-500元/天"
    assert a.raw["days_per_week"] == 4 and a.raw["min_months"] == 6   # 来自列表 daysPerWeekDesc/leastMonthDesc
    assert a.jd_text == "到岗要求：4天/周 6个月"
    assert a.raw["detail_skipped"] == "error" and "detail_skipped" not in b.raw   # 读不到详情的打标，下次抓取再补

    assert b.platform_id == "FAKEBOSSJOBALPHA02"
    assert b.company == "星河科技"
    assert b.location == "深圳"
    assert b.raw["days_per_week"] == 4 and b.raw["min_months"] == 4
    assert b.jd_text.startswith("团队介绍")
    assert b.jd_text.endswith("到岗要求：4天/周 4个月")
    assert "福利：交通补贴" in b.jd_text
    assert b.raw["industry"] == "互联网"               # detail 里没有 industry，退回列表行
    assert b.raw["company_intro"].startswith("星河科技成立于")


def test_detail_limit_caps_detail_calls():
    f = Fake()
    jobs = _adapter(f).search(_query(detail_limit=1), _limiter(), log=lambda s: None)
    assert len(jobs) == 1
    detail_calls = [e for e in f.evals if e[2] == DETAIL_DOM_JS and e[1] is not None]   # 只数导航到岗位页的次数（同页重读不算）
    assert len(detail_calls) == 1


def test_hard_prefilter_skips_detail_but_returns_list_row():
    """列表自带到岗天数/时长：超出硬条件上限的不花详情预算，但仍以列表字段返回，让 prescore 记下硬伤原因。"""
    f = Fake()
    q = _query(extra={"experience": "在校生", "job_type": "实习", "max_days_per_week": 4, "max_min_months": 5})
    jobs = _adapter(f).search(q, _limiter(), log=lambda s: None)
    detail_urls = [e[1] for e in f.evals if e[2] == DETAIL_DOM_JS]
    assert detail_urls == [DOUBAO_JOB_URL]                      # 多模态是 6 个月 > 5，跳过详情
    by_id = {j.platform_id: j for j in jobs}
    multi = by_id["FAKEBOSSJOBALPHA01"]
    assert multi.raw["detail_skipped"] == "hard_prefilter" and multi.raw["min_months"] == 6
    assert multi.jd_text == "到岗要求：4天/周 6个月"
    assert "detail_skipped" not in by_id["FAKEBOSSJOBALPHA02"].raw


def test_known_ids_skip_detail_and_do_not_consume_budget():
    """已入库的岗位不再抓详情（仍返回列表行让 ingest 记"已见"），详情预算留给新岗位。"""
    f = Fake()
    q = _query(detail_limit=1, extra={"experience": "在校生", "job_type": "实习", "known_ids": {"FAKEBOSSJOBALPHA01"}})
    jobs = _adapter(f).search(q, _limiter(), log=lambda s: None)
    detail_urls = [e[1] for e in f.evals if e[2] == DETAIL_DOM_JS]
    assert detail_urls == [DOUBAO_JOB_URL]                      # 预算 1 条给了未入库的示例岗位，而不是已知的多模态
    by_id = {j.platform_id: j for j in jobs}
    assert by_id["FAKEBOSSJOBALPHA01"].raw["detail_skipped"] == "known"
    assert by_id["FAKEBOSSJOBALPHA02"].company == "星河科技"


def test_not_logged_in_raises_before_any_eval():
    f = Fake(logged_in=False)
    with pytest.raises(AuthRequiredError):
        _adapter(f).search(_query(), _limiter(), log=lambda s: None)
    assert f.evals == []


def test_call_code_7_is_auth_required_and_closes_session():
    f = Fake()
    f.evaluate = _fixed_json_response(json.dumps({"code": 7, "message": "Cookie 已过期", "zpData": {}}))
    with pytest.raises(AuthRequiredError):
        _adapter(f).search(_query(), _limiter(), log=lambda s: None)
    assert f.closed == ["jp-boss"]


def test_code_37_triggers_page_refresh_then_succeeds():
    # 真机实测：joblist.json 遇 code 37（"您的环境存在异常."）意味着 __zp_stoken__ 令牌过期，
    # 在同一会话里重新打开入口页（entry_url）会让站方 JS 自己刷新令牌，重试同一请求即可恢复。
    f = Fake(code37_on_first_joblist=True)
    lim = _limiter()
    jobs = _adapter(f).search(_query(), lim, log=lambda s: None)

    joblist_evals = [e for e in f.evals if "joblist.json" in e[2]]
    assert len(joblist_evals) >= 2
    assert joblist_evals[0][1] is None              # 第一次沿用 origin 检查时已打开的会话
    href_evals = [e for e in f.evals if e[2] == "location.href"]
    entry_url = href_evals[0][1]
    assert entry_url is not None and entry_url.startswith("https://www.zhipin.com/web/geek/jobs?")
    assert "experience=108" in entry_url
    assert joblist_evals[1][1] == entry_url         # 37 触发的重试改用 entry_url 重新导航（不是 None）

    assert len(jobs) == 2
    a, b = jobs
    assert a.platform_id == "FAKEBOSSJOBALPHA01"
    assert b.platform_id == "FAKEBOSSJOBALPHA02"

    # 正常路径 4 次 wait（1 页列表 + 1 页空列表 + 2 条详情）+ 37 触发的额外 1 次 wait = 5。
    assert lim.calls == 5
    assert f.closed == ["jp-boss"]


def test_refresh_budget_resets_on_each_success_across_keywords():
    # 旧 bug：self._refreshes 只在 search() 开头清零一次，跨整次搜索累计。4 个关键词各自都在第一次
    # 请求时遇到一次 code 37（紧接着刷新重试就成功），旧逻辑会把 4 次刷新累计成 4 > MAX_TOKEN_REFRESHES(3)，
    # 误判成风控连续异常；正确语义是"连续刷新超过 3 次才算风控"——每次成功都应该把计数清零。
    f = Fake()
    calls = {"n": 0}

    def evaluate(session, url, js, **kw):
        if js == "location.href":
            return ZHIPIN_HREF
        if js == DETAIL_DOM_JS:
            return "{}"
        if "joblist.json" in js:
            calls["n"] += 1
            if calls["n"] % 2 == 1:      # 每个关键词的第一次请求：令牌过期
                return json.dumps({"code": 37, "message": "您的环境存在异常."})
            return JOBLIST               # 刷新后重试：成功
        raise AssertionError(js)

    f.evaluate = evaluate
    q = _query(keywords=["kw1", "kw2", "kw3", "kw4"], max_pages=1)
    jobs = _adapter(f).search(q, _limiter(), log=lambda s: None)   # 不应抛 RiskControlError

    assert calls["n"] == 8   # 4 个关键词 × (37 一次 + 重试成功一次)
    assert len(jobs) == 2
    assert f.closed == ["jp-boss"]


def test_code_37_twice_is_risk_control():
    # 重新导航刷新令牌后仍是 37 → 不是"没登录"也不是瞬时抖动，按风控处理并停止、不再重试。
    f = Fake()
    f.evaluate = _fixed_json_response(json.dumps({"code": 37, "message": "您的环境存在异常."}))
    with pytest.raises(RiskControlError):
        _adapter(f).search(_query(), _limiter(), log=lambda s: None)
    assert f.closed == ["jp-boss"]


def test_call_risk_message_is_risk_control_and_closes_session():
    f = Fake()
    f.evaluate = _fixed_json_response(json.dumps({"code": 5, "message": "操作频繁，请稍后再试", "zpData": {}}))
    with pytest.raises(RiskControlError):
        _adapter(f).search(_query(), _limiter(), log=lambda s: None)
    assert f.closed == ["jp-boss"]


def test_call_other_code_is_plain_adapter_error_and_closes_session():
    f = Fake()
    f.evaluate = _fixed_json_response(json.dumps({"code": 9, "message": "参数错误", "zpData": {}}))
    with pytest.raises(AdapterError) as exc_info:
        _adapter(f).search(_query(), _limiter(), log=lambda s: None)
    assert type(exc_info.value) is AdapterError   # 不是 AuthRequiredError / RiskControlError 子类
    assert f.closed == ["jp-boss"]


def test_xhr_error_retries_once_with_reopen_then_succeeds():
    # 真机实测：共享的 "OpenCLI Browser" 标签页偶发 open 之后仍停在 about:blank，同源 XHR 直接网络
    # 失败（Promise 里 x.onerror 拒绝成 'xhr error'）；重新导航入口页后重放同一个请求就能恢复。
    f = Fake(xhr_error_on_first_joblist=True)
    lim = _limiter()
    jobs = _adapter(f).search(_query(), lim, log=lambda s: None)

    joblist_evals = [e for e in f.evals if "joblist.json" in e[2]]
    assert len(joblist_evals) >= 2
    assert joblist_evals[0][1] is None
    href_evals = [e for e in f.evals if e[2] == "location.href"]
    entry_url = href_evals[0][1]
    assert entry_url is not None and entry_url.startswith("https://www.zhipin.com/web/geek/jobs?")
    assert joblist_evals[1][1] == entry_url   # xhr error 触发的重试用 entry_url 重新导航

    assert len(jobs) == 2
    assert lim.calls == 5   # 基线 4 + xhr error 触发的额外 1 次 wait
    assert f.closed == ["jp-boss"]


def test_xhr_error_twice_propagates_adapter_error():
    def evaluate(session, url, js, **kw):
        if js == "location.href":
            return ZHIPIN_HREF
        raise AdapterError("opencli browser eval 无输出: ✖  Error: xhr error")
    f = Fake()
    f.evaluate = evaluate
    with pytest.raises(AdapterError) as exc_info:
        _adapter(f).search(_query(), _limiter(), log=lambda s: None)
    assert "xhr error" in str(exc_info.value)
    assert type(exc_info.value) is AdapterError   # 重试仍失败按原样传播，不转成风控
    assert f.closed == ["jp-boss"]


def test_origin_check_reopens_when_page_not_on_zhipin():
    # 真机观测到过 open 之后 location.href 还是 about:blank；第一次检查没过就重开一次入口页再查一遍。
    f = Fake(blank_first_href=True)
    lim = _limiter()
    jobs = _adapter(f).search(_query(), lim, log=lambda s: None)

    href_evals = [e for e in f.evals if e[2] == "location.href"]
    assert len(href_evals) == 2
    entry_url = href_evals[0][1]
    assert entry_url is not None and entry_url.startswith("https://www.zhipin.com/web/geek/jobs?")
    assert href_evals[1][1] == entry_url   # 重开用的是同一个 entry_url

    assert len(jobs) == 2
    assert lim.calls == 5   # 基线 4 + origin 检查失败触发的额外 1 次 wait
    assert f.closed == ["jp-boss"]


def test_daemon_restart_recovers_a_wedged_session():
    """真机：整夜连抓时第二个查询块的标签页卡死在 about:blank，重开多少次都没用——重启守护进程换新租约才恢复。"""
    f = Fake()
    f.blank_hrefs = 3               # 三次确认全是空白页，逼出守护进程重启
    jobs = _adapter(f).search(_query(), _limiter(), log=lambda s: None)
    assert f.restarts == 1 and len(jobs) == 2
    assert len([e for e in f.evals if e[2] == "location.href"]) == 4    # 3 次确认 + 重启后再确认 1 次


def test_wedged_session_that_restart_cannot_fix_raises():
    f = Fake()
    f.blank_hrefs = 99              # 重启也救不回来
    def restart(**kw):
        f.restarts += 1
        return True
    a = BossAdapter(run=f.run, evaluate=f.evaluate, close=f.close, sleep=lambda s: None, restart_daemon=restart)
    with pytest.raises(AdapterError) as exc_info:
        a.search(_query(), _limiter(), log=lambda s: None)
    assert "页面未就绪" in str(exc_info.value) and f.restarts == 1
    assert f.closed == ["jp-boss"]


def test_detail_first_read_empty_then_rendered():
    """详情页首读为空（JS 未渲染完）→ 等待后同页重读拿到内容，不再退回列表字段。"""
    f = Fake()
    reads = {"n": 0}
    def evaluate(session, url, js, **kw):
        if js == "location.href":
            return ZHIPIN_HREF
        if js == DETAIL_DOM_JS:
            reads["n"] += 1
            return "{}" if url is not None else DOM_FIXTURE      # 带 url 的首读为空；同页重读（url=None）拿到夹具
        if "joblist.json" in js:
            return JOBLIST if '"page": "1"' in js else EMPTY_LIST
        raise AssertionError(js)
    f.evaluate = evaluate
    slept = []
    a = BossAdapter(run=f.run, evaluate=f.evaluate, close=f.close, sleep=slept.append)
    jobs = a.search(_query(), _limiter(), log=lambda s: None)
    assert reads["n"] == 4 and slept == [2.5, 2.5]
    assert all(j.company == "星河科技" and "detail_skipped" not in j.raw for j in jobs)


def test_detail_dom_non_json_falls_back_to_row_without_raising():
    f = Fake()
    def evaluate(session, url, js, **kw):
        if js == "location.href":
            return ZHIPIN_HREF
        if js == DETAIL_DOM_JS:
            return "not json at all"   # 页面还没渲染出来 / 结构完全变了，两种都当成解析失败
        if "joblist.json" in js:
            return JOBLIST if '"page": "1"' in js else EMPTY_LIST
        raise AssertionError(js)
    f.evaluate = evaluate
    jobs = _adapter(f).search(_query(), _limiter(), log=lambda s: None)
    # 两条详情页都解析失败，退回列表字段——不抛异常、不丢数据。
    assert len(jobs) == 2
    assert all(j.title for j in jobs)
    assert f.closed == ["jp-boss"]


def test_detail_dom_risky_title_raises_risk_control():
    f = Fake()
    def evaluate(session, url, js, **kw):
        if js == "location.href":
            return ZHIPIN_HREF
        if js == DETAIL_DOM_JS:
            return json.dumps({"name": "x", "title": "请完成安全验证后继续", "sections": [{"h": "x", "text": "y"}]})
        if "joblist.json" in js:
            return JOBLIST if '"page": "1"' in js else EMPTY_LIST
        raise AssertionError(js)
    f.evaluate = evaluate
    with pytest.raises(RiskControlError):
        _adapter(f).search(_query(), _limiter(), log=lambda s: None)
    assert f.closed == ["jp-boss"]
