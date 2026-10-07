from __future__ import annotations
import json
import pathlib
import pytest
from jp.adapters.base import AuthRequiredError, AdapterError, RateLimiter, SearchQuery
from jp.adapters.nowcoder import NowcoderAdapter, build_js, to_rawjob

FIX = pathlib.Path(__file__).resolve().parent / "fixtures" / "adapters"
PAGE1 = (FIX / "nowcoder_square_search.json").read_text(encoding="utf-8")
EMPTY = json.dumps({"code": 0, "msg": "OK", "data": {"totalCount": 45, "totalPage": 15, "currentPage": 2, "datas": []}})


class Fake:
    def __init__(self, logged_in=True):
        self.runs, self.evals, self.closed = [], [], []
        self.logged_in = logged_in
    def run(self, args, **kw):
        self.runs.append(args)
        if not self.logged_in:
            raise AuthRequiredError("Nowcoder t cookie missing")
        return {"logged_in": True, "site": "nowcoder"}
    def evaluate(self, session, url, js, **kw):
        self.evals.append((session, url, js))
        return PAGE1 if '"page": "1"' in js or '"page":"1"' in js else EMPTY
    def close(self, session, **kw):
        self.closed.append(session)


def _adapter(f):
    return NowcoderAdapter(run=f.run, evaluate=f.evaluate, close=f.close)


def test_build_js_recruit_type_and_graduation_year():
    js = build_js("算法", "成都", 1, 20, recruit_type="1")
    assert '"recruitType": "1"' in js and '"jobCity": "成都"' in js
    assert '"recruitType": "2"' in build_js("算法", "深圳", 1, 20)
    d = {"id": 1, "jobName": "AI算法工程师-2027届", "jobCity": "深圳", "recruitType": 1, "graduationYear": "2027届",
         "durationDays": 0, "durationMonths": 0, "ext": "{}", "recommendInternCompany": {"companyName": "C"}}
    rj = to_rawjob(d)
    assert rj.raw["graduation_year"] == "2027届" and rj.raw["recruit_type"] == "校招"
    assert rj.raw["days_per_week"] == 0 and "到岗要求" not in rj.jd_text


def test_build_js_has_no_percent_and_carries_query():
    js = build_js("大模型", "深圳", 1, 20)
    assert "%" not in js and "square-search" in js and "XMLHttpRequest" in js
    assert '"query": "大模型"' in js and '"jobCity": "深圳"' in js and '"recruitType": "2"' in js


def test_search_pages_until_empty_and_maps_fields():
    f = Fake()
    q = SearchQuery(region="CN", keywords=["大模型"], city="深圳", max_pages=3, page_size=20)
    lim = RateLimiter(0, 0, sleep=lambda s: None)
    jobs = _adapter(f).search(q, lim, log=lambda s: None)
    assert f.runs[0][:2] == ["nowcoder", "whoami"]
    assert f.evals[0][1] == "https://www.nowcoder.com/jobs/intern/center" and f.evals[1][1] is None
    assert len(f.evals) == 2 and f.closed == ["jp-nowcoder"]
    assert lim.calls == 2
    a, b = jobs
    assert a.platform_id == "700300001" and a.title == "AI 产品经理（实习）" and a.company == "示例科技"
    assert a.url == "https://www.nowcoder.com/jobs/detail/700300001" and a.location == "深圳"
    assert a.salary_raw == "350-400元/天" and a.posted_at == "2030-02-03"
    assert a.raw["days_per_week"] == 5 and a.raw["min_months"] == 3 and a.raw["job_offer"] == 1
    assert "任职要求：" in a.jd_text and "岗位职责：" in a.jd_text and "到岗要求：每周 5 天，至少 3 个月，有转正机会" in a.jd_text
    assert b.platform_id == "700300002" and b.company == "云杉数据" and b.raw["days_per_week"] == 4


def test_not_logged_in_raises_before_any_eval():
    f = Fake(logged_in=False)
    with pytest.raises(AuthRequiredError):
        _adapter(f).search(SearchQuery(region="CN", keywords=["x"], city="深圳"), RateLimiter(0, 0, sleep=lambda s: None), log=lambda s: None)
    assert f.evals == []


def test_api_error_code_is_adapter_error():
    f = Fake()
    f.evaluate = lambda session, url, js, **kw: json.dumps({"code": 999, "msg": "参数错误", "data": None})
    with pytest.raises(AdapterError):
        _adapter(f).search(SearchQuery(region="CN", keywords=["x"], city="深圳"), RateLimiter(0, 0, sleep=lambda s: None), log=lambda s: None)


def test_to_rawjob_missing_company_and_ext():
    rj = to_rawjob({"id": 1, "jobName": "t", "ext": "not json", "jobCity": "深圳"})
    assert rj.company == "(待抽取)" and rj.jd_text == "" and rj.platform_id == "1"


def test_to_rawjob_ext_as_dict_does_not_raise():
    rj = to_rawjob({"id": 2, "jobName": "t2", "ext": {"infos": "已经是 dict，不是字符串"}, "jobCity": "上海"})
    assert rj.platform_id == "2" and rj.jd_text == ""
