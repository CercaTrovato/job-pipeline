from __future__ import annotations
import json
import pathlib
import pytest
from jp.adapters import watchlist as wl
from jp.adapters.base import RateLimiter
from jp.adapters.watchlist import bamboohr, pinpoint, workday
from jp.adapters.watchlist.registry import FETCHERS

FIX = pathlib.Path(__file__).resolve().parent / "fixtures" / "adapters"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class FakeHttp:
    """按 (method, url 前缀/后缀) 返回夹具；记录调用。"""
    def __init__(self, table):
        self.table, self.calls = table, []
    def _find(self, method, url):
        for (m, needle), val in self.table.items():
            if m == method and needle in url:
                return val
        raise AssertionError("unexpected %s %s" % (method, url))
    def get_json(self, url, headers=None):
        self.calls.append(("GET", url)); return self._find("GET", url)
    def post_json(self, url, payload, headers=None):
        self.calls.append(("POST", url, payload)); return self._find("POST", url)
    def get_text(self, url, headers=None):
        self.calls.append(("GET", url)); return self._find("GET", url)


def _entry(ats, params, keywords, region="HK", company="X"):
    return wl.Entry(company=company, region=region, tier="大厂", grade="A", careers_url="https://x", ats=ats,
                    keywords=keywords, enabled=True, params=params, note="")


def _lim():
    return RateLimiter(0, 0, sleep=lambda s: None)


def test_registry_has_three_fetchers():
    assert {"workday", "bamboohr", "pinpoint"} <= set(FETCHERS)


def test_workday_list_then_detail_with_region_postfilter():
    http = FakeHttp({("POST", "/wday/cxs/sampleco/ExampleCareers/jobs"): load("workday_list.json"),
                     ("GET", "FAKE-REQ-001"): load("workday_detail.json"),
                     ("GET", "FAKE-REQ-002"): {}})
    e = _entry("workday", {"tenant": "sampleco", "site": "ExampleCareers"}, ["Intern"], company="ExampleCo")
    jobs = workday.fetch_company(e, http, {"max_pages": 1, "detail_limit": 30}, _lim(), lambda s: None)
    posts = [c for c in http.calls if c[0] == "POST"]
    assert len(posts) == 1 and posts[0][2] == {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "intern"}
    a, b = jobs
    assert a.platform_id == "workday:sampleco:FAKE-REQ-001" and a.title == "ExampleCo 2027 Summer Internship Programme" and a.company == "ExampleCo"
    assert a.url.startswith("https://sampleco.wd3.myworkdayjobs.com/ExampleCareers/job/") and a.location == "Hong Kong SAR" and a.posted_at == "2030-06-01"
    assert len(a.jd_text) > 200 and "<" not in a.jd_text and a.raw["time_type"] == "Full time" and a.raw["country"] == "Hong Kong"
    assert b.platform_id == "workday:sampleco:FAKE-REQ-002" and b.jd_text == "" and b.url == "https://sampleco.wd3.myworkdayjobs.com/ExampleCareers/job/Hong-Kong/ExampleCo-Summer-Internship-FAKE-REQ-002"


def test_workday_uses_location_facet_when_present():
    listing = load("workday_list.json")
    listing["facets"] = [{"facetParameter": "locationCountry", "values": [{"descriptor": "Philippines", "id": "ph1", "count": 9}, {"descriptor": "Hong Kong SAR", "id": "hk1", "count": 3}]}]
    http = FakeHttp({("POST", "/wday/cxs/sampleco/External/jobs"): listing, ("GET", "FAKE-REQ-"): {}})
    e = _entry("workday", {"tenant": "sampleco", "site": "External"}, ["Intern"], company="ExampleCo")
    jobs = workday.fetch_company(e, http, {"max_pages": 1, "detail_limit": 30}, _lim(), lambda s: None)
    posts = [c for c in http.calls if c[0] == "POST"]
    assert len(posts) == 2 and posts[1][2]["appliedFacets"] == {"locationCountry": ["hk1"]}
    assert len(jobs) == 2   # 有 facet 时不再按 locationsText 后过滤


def test_workday_postfilter_drops_other_regions():
    listing = load("workday_list.json")
    listing["jobPostings"][1]["locationsText"] = "Makati, PH-ExampleCo Philippines"
    http = FakeHttp({("POST", "/jobs"): listing, ("GET", "FAKE-REQ-001"): load("workday_detail.json")})
    e = _entry("workday", {"tenant": "sampleco", "site": "ExampleCareers"}, ["Intern"])
    jobs = workday.fetch_company(e, http, {"max_pages": 1, "detail_limit": 30}, _lim(), lambda s: None)
    assert [j.platform_id for j in jobs] == ["workday:sampleco:FAKE-REQ-001"]


def test_workday_postfilter_checks_every_page_before_stopping():
    """无地区 facet 的 tenant：第 1 页（offset 0）两条都不在目标地区，第 2 页（offset 20）才有一条命中——
    旧实现在过滤挪到循环外之前，"仍会把两页都取来"，但过滤是在整个循环结束后才做的一次性操作；这里要
    验证的是"边翻页边过滤"且不会因为某一页 0 命中就提前停（只有整页 0 条原始记录，或 offset 超过
    total，或已经攒够 detail_limit 才停）。"""
    listing = load("workday_list.json")
    listing["jobPostings"][0]["locationsText"] = "Makati, PH-ExampleCo Philippines"
    listing["jobPostings"][1]["locationsText"] = "Makati, PH-ExampleCo Philippines"
    listing["total"] = 25   # 让 offset=20（第 2 页）< total，但 offset=40（第 3 页）>= total，不用真的喂第 3 页夹具
    page2 = json.loads(json.dumps(listing))   # 独立深拷贝，避免与 page1 共享同一个 dict
    hk_posting = dict(page2["jobPostings"][0])
    hk_posting["locationsText"] = "Hong Kong SAR"
    hk_posting["externalPath"] = "/job/HK-TWO-ES-8F/HKEX-2027-Summer-Internship-Programme_FAKE-REQ-003"
    hk_posting["bulletFields"] = ["FAKE-REQ-003"]
    page2["jobPostings"] = [hk_posting]

    class OffsetFakeHttp(FakeHttp):
        """按 POST payload 里的 offset 分派：0 → 第 1 页（两条都非目标地区），20 → 第 2 页（一条命中）。"""
        def post_json(self, url, payload, headers=None):
            self.calls.append(("POST", url, payload))
            return page2 if payload.get("offset") else listing

    http = OffsetFakeHttp({("GET", "FAKE-REQ-003"): {}})
    e = _entry("workday", {"tenant": "sampleco", "site": "ExampleCareers"}, ["Intern"], company="ExampleCo")
    jobs = workday.fetch_company(e, http, {"max_pages": 3, "detail_limit": 2}, _lim(), lambda s: None)

    posts = [c for c in http.calls if c[0] == "POST"]
    gets = [c for c in http.calls if c[0] == "GET"]
    assert len(posts) == 2 and len(gets) == 1        # 第 3 页 offset(40) >= total(25)，不该再请求
    assert [j.platform_id for j in jobs] == ["workday:sampleco:FAKE-REQ-003"]


def test_bamboohr_filters_by_title_then_detail():
    http = FakeHttp({("GET", "sampleco.bamboohr.com/careers/list"): load("bamboohr_list.json"),
                     ("GET", "/careers/600400001/detail"): load("bamboohr_detail.json"),
                     ("GET", "/careers/600400002/detail"): {}})
    e = _entry("bamboohr", {"sub": "sampleco"}, ["Officer"], company="ExampleCo")
    jobs = bamboohr.fetch_company(e, http, {"max_pages": 3, "detail_limit": 30}, _lim(), lambda s: None)
    a, b = jobs
    assert a.platform_id == "bamboohr:sampleco:600400001" and a.url == "https://careers.example.test/careers/600400001" and a.company == "ExampleCo"
    assert a.location == "Hong Kong, Hong Kong SAR" and a.posted_at == "2030-02-03" and len(a.jd_text) > 200 and a.raw["employment"] == "Full-Time"
    assert b.platform_id == "bamboohr:sampleco:600400002" and b.url == "https://sampleco.bamboohr.com/careers/600400002" and b.jd_text == ""
    e2 = _entry("bamboohr", {"sub": "sampleco"}, ["Intern"])
    assert bamboohr.fetch_company(e2, FakeHttp({("GET", "/careers/list"): load("bamboohr_list.json")}), {}, _lim(), lambda s: None) == []


def test_pinpoint_filters_and_maps():
    http = FakeHttp({("GET", "sampleco.pinpointhq.com/postings.json"): load("pinpoint_postings.json")})
    e = _entry("pinpoint", {"sub": "sampleco"}, ["Analyst"], company="ExampleCo")
    jobs = pinpoint.fetch_company(e, http, {}, _lim(), lambda s: None)
    assert len(jobs) == 1
    j = jobs[0]
    assert j.platform_id == "pinpoint:sampleco:500500001" and j.title.startswith("Senior Analyst") and j.company == "ExampleCo"
    assert j.url.startswith("https://jobs.example.test/en/postings/") and j.location == "Hong Kong (SAR)"
    assert "<" not in j.jd_text and len(j.jd_text) > 300 and j.raw["employment_type"] == "permanent_full_time" and j.raw["deadline_at"].startswith("2030-05-30")
    listing = load("pinpoint_postings.json")
    listing["data"][1]["employment_type"] = "internship"
    jobs2 = pinpoint.fetch_company(_entry("pinpoint", {"sub": "sampleco"}, ["nothing"]), FakeHttp({("GET", "postings.json"): listing}), {}, _lim(), lambda s: None)
    assert [x.platform_id for x in jobs2] == ["pinpoint:sampleco:500500002"]   # employment_type 含 intern 也算


from jp.adapters.watchlist import feishu, workatsea, zhiye


def test_registry_has_six_fetchers():
    assert {"workday", "bamboohr", "pinpoint", "feishu", "beisen", "workatsea"} <= set(FETCHERS)


def test_feishu_csrf_then_posts_with_headers():
    posts = load("feishu_posts.json")
    http = FakeHttp({("POST", "/api/v1/csrf/token"): {"code": 0, "data": {"token": "tok123"}},
                     ("POST", "/api/v1/search/job/posts"): posts})
    e = _entry("feishu", {"host": "sampleco.jobs.feishu.cn", "site": "sample-campus", "portal_type": 6}, ["实习"], region="CN", company="示例科技")
    jobs = feishu.fetch_company(e, http, {"max_pages": 3}, _lim(), lambda s: None)
    csrf, search = [c for c in http.calls if c[0] == "POST"][:2]
    assert csrf[1] == "https://sampleco.jobs.feishu.cn/api/v1/csrf/token" and csrf[2] == {}
    assert search[1].startswith("https://sampleco.jobs.feishu.cn/api/v1/search/job/posts?") and "portal_type=6" in search[1] and "limit=50" in search[1]
    assert search[2]["portal_type"] == 6 and search[2]["limit"] == 50 and search[2]["offset"] == 0 and search[2]["keyword"] == ""
    assert len([c for c in http.calls if "search/job/posts" in c[1]]) == 1      # count=2 ≤ 50 → 一页
    a, b = jobs
    assert a.platform_id == "feishu:sampleco.jobs.feishu.cn:SYN-FEISHU-POST-01" and a.title == "算法实习生（合成样本）"
    assert a.company == "示例科技" and a.url == "https://sampleco.jobs.feishu.cn/sample-campus/position/SYN-FEISHU-POST-01/detail"
    assert a.location == "深圳" and a.posted_at == "2030-02-03" and a.raw["recruit_type"] == "实习" and a.raw["category"] == "研发"
    assert a.jd_text.startswith("岗位描述：") and "任职要求：" in a.jd_text and "每周实习5天" in a.jd_text
    assert b.platform_id.endswith(":SYN-FEISHU-POST-02")


def test_feishu_headers_carry_site_and_token(monkeypatch):
    seen = {}
    class H(FakeHttp):
        def post_json(self, url, payload, headers=None):
            seen[url.split("?")[0]] = dict(headers or {})
            return super().post_json(url, payload, headers)
    http = H({("POST", "csrf/token"): {"code": 0, "data": {"token": "tok123"}}, ("POST", "search/job/posts"): {"code": 0, "data": {"job_post_list": [], "count": 0}}})
    e = _entry("feishu", {"host": "sampleco-one.jobs.feishu.cn", "site": "index", "portal_type": 2}, ["实习"], region="CN")
    assert feishu.fetch_company(e, http, {}, _lim(), lambda s: None) == []
    h = seen["https://sampleco-one.jobs.feishu.cn/api/v1/search/job/posts"]
    assert h["website-path"] == "index" and h["Portal-Channel"] == "saas-career" and h["Portal-Platform"] == "pc" and h["x-csrf-token"] == "tok123"


def test_feishu_api_error_is_adapter_error():
    from jp.adapters.base import AdapterError
    http = FakeHttp({("POST", "csrf/token"): {"code": 0, "data": {"token": "t"}}, ("POST", "search/job/posts"): {"code": -9000003, "message": "site not exist", "data": None}})
    with pytest.raises(AdapterError):
        feishu.fetch_company(_entry("feishu", {"host": "sampleco-two.jobs.feishu.cn", "site": "index", "portal_type": 2}, ["实习"], region="CN"), http, {}, _lim(), lambda s: None)


def test_zhiye_pages_until_short_page():
    listing = load("zhiye_list.json")
    http = FakeHttp({("POST", "sampleco.zhiye.com/api/Jobad/GetJobAdPageList"): listing})
    e = _entry("beisen", {"host": "sampleco.zhiye.com", "category_id": 2}, ["算法"], region="CN", company="示例科技")
    jobs = zhiye.fetch_company(e, http, {"max_pages": 3}, _lim(), lambda s: None)
    posts = [c for c in http.calls if c[0] == "POST"]
    assert len(posts) == 1 and posts[0][2] == {"PageIndex": 1, "PageSize": 20, "Keyword": "", "CategoryId": 2}   # 夹具只有 2 条 < 20 → 不翻页
    a, b = jobs
    assert a.platform_id == "beisen:sampleco.zhiye.com:FAKE-ZY-001" and a.title == "高级系统开发工程师（算法工程化）(J00001)" and a.company == "示例科技"
    assert a.url == "https://sampleco.zhiye.com/campus/jobs#jobAdId=FAKE-ZY-001" and a.location == "" and a.posted_at == ""
    assert a.jd_text.startswith("岗位职责：") and "任职要求：" in a.jd_text and len(a.jd_text) > 300
    assert b.platform_id.endswith(":FAKE-ZY-002")


def test_zhiye_non_200_code_is_adapter_error():
    from jp.adapters.base import AdapterError
    http = FakeHttp({("POST", "GetJobAdPageList"): {"Code": 500, "Message": "参数错误", "Data": None}})
    with pytest.raises(AdapterError):
        zhiye.fetch_company(_entry("beisen", {"host": "x.zhiye.com"}, ["x"], region="CN"), http, {}, _lim(), lambda s: None)


def test_zhiye_max_pages_zero_returns_empty_without_request():
    http = FakeHttp({})
    e = _entry("beisen", {"host": "x.zhiye.com", "max_pages": 0}, ["x"], region="CN")
    jobs = zhiye.fetch_company(e, http, {}, _lim(), lambda s: None)
    assert jobs == [] and http.calls == []


def test_workatsea_filters_city_and_maps():
    http = FakeHttp({("GET", "/user/meta/slice/"): load("workatsea_meta.json"),
                     ("GET", "/user/job/list/"): load("workatsea_list.json")})
    e = _entry("workatsea", {"city_ids": [6], "max_pages": 6}, ["Intern"], region="CN", company="ExampleCo")
    jobs = workatsea.fetch_company(e, http, {"max_pages": 3}, _lim(), lambda s: None)
    gets = [c[1] for c in http.calls]
    assert gets[0].endswith("/user/meta/slice/?flags=2147851120&from_career=true") and "limit=500&offset=0" in gets[1]
    assert len(jobs) == 1
    j = jobs[0]
    assert j.platform_id == "workatsea:FAKE-JOB-002" and j.title == "Data Science Intern (Shenzhen)" and j.company == "ExampleCo"
    assert j.url.endswith("/jobs?keyword=FAKE-JOB-002") and j.location == "Shenzhen, China"
    assert "LLM-based analytics" in j.jd_text and "Python, SQL" in j.jd_text and "<" not in j.jd_text
    assert j.raw["employment_id"] == 3 and j.raw["city_id"] == 6
