from __future__ import annotations
import json
import pathlib
from jp.adapters.base import RateLimiter, SearchQuery
from jp.adapters.jobsdb import JobsdbAdapter, SEARCH_URL, JOB_URL, parse_detail, to_rawjob

FIX = pathlib.Path(__file__).resolve().parent / "fixtures" / "adapters"
SEARCH = json.loads((FIX / "jobsdb_search.json").read_text(encoding="utf-8"))
DETAIL_HTML = (FIX / "jobsdb_detail.html").read_text(encoding="utf-8")


class FakeHttp:
    def __init__(self):
        self.urls = []
    def get_json(self, url, headers=None):
        self.urls.append(url)
        assert url.startswith(SEARCH_URL)
        return SEARCH if "page=1&" in url or url.endswith("page=1") else {"data": [], "totalCount": SEARCH["totalCount"]}
    def get_text(self, url, headers=None):
        self.urls.append(url)
        return DETAIL_HTML if url == JOB_URL % "800200001" else "<html><body>changed layout</body></html>"


def test_parse_detail_reads_apollo_job():
    d = parse_detail(DETAIL_HTML)
    assert d["is_link_out"] is True and d["expires_at"] == "2030-05-30" and d["posted_at"] == "2030-02-03"
    assert d["work_type"] == "Full time" and d["location"] == "Hong Kong SAR" and d["advertiser"] == "Harbor Analytics Limited"
    assert len(d["description"]) > 500 and "<" not in d["description"] and "&nbsp;" not in d["description"]
    assert parse_detail("<html>no apollo</html>") == {}


def test_search_pages_and_detail_merge():
    http = FakeHttp()
    q = SearchQuery(region="HK", keywords=["intern"], max_pages=3, page_size=30, detail_limit=40)
    lim = RateLimiter(0, 0, sleep=lambda s: None)
    jobs = JobsdbAdapter(http=http).search(q, lim, log=lambda s: None)
    assert http.urls[0].startswith(SEARCH_URL) and "keywords=intern" in http.urls[0] and "page=1" in http.urls[0] and "pageSize=30" in http.urls[0]
    assert sum(1 for u in http.urls if u.startswith(SEARCH_URL)) == 1     # 第一页只有 2 条（< pageSize）→ 不翻页
    assert lim.calls == 3
    a, b = jobs
    assert a.platform_id == "800200001" and a.title == "Program Intern" and a.company == "Harbor Analytics"
    assert a.url == "https://hk.jobsdb.com/job/800200001" and a.location == "Hong Kong SAR" and a.posted_at == "2030-02-03"
    assert len(a.jd_text) > 500 and a.raw["is_link_out"] is True and a.raw["work_types"] == ["Full time"] and a.raw["work_arrangement"] == "Hybrid"
    # 第二条详情页结构变了 → 退回 teaser + bulletPoints
    assert b.platform_id == "800200002" and b.company == "Northstar Devices" and b.jd_text.startswith(SEARCH["data"][1]["teaser"][:30])
    assert b.raw["is_link_out"] is None


def test_known_ids_skip_detail_page_but_still_returned():
    http = FakeHttp()
    q = SearchQuery(region="HK", keywords=["intern"], max_pages=1, page_size=30, detail_limit=1, extra={"known_ids": {"800200001"}})
    jobs = JobsdbAdapter(http=http).search(q, RateLimiter(0, 0, sleep=lambda s: None), log=lambda s: None)
    detail_urls = [u for u in http.urls if not u.startswith(SEARCH_URL)]
    assert detail_urls == [JOB_URL % "800200002"]                 # 预算给了未入库的 Northstar Devices 那条
    by_id = {j.platform_id: j for j in jobs}
    assert by_id["800200001"].raw["detail_skipped"] == "known" and by_id["800200002"].company == "Northstar Devices"


def test_detail_http_error_falls_back_to_list_row():
    from jp.adapters.base import AdapterError

    class Flaky(FakeHttp):
        def get_text(self, url, headers=None):
            if url == JOB_URL % "800200001":
                raise AdapterError("HTTP 404")
            return super().get_text(url, headers)

    q = SearchQuery(region="HK", keywords=["intern"], max_pages=1, page_size=30, detail_limit=40)
    jobs = JobsdbAdapter(http=Flaky()).search(q, RateLimiter(0, 0, sleep=lambda s: None), log=lambda s: None)
    by_id = {j.platform_id: j for j in jobs}
    assert by_id["800200001"].raw["detail_skipped"] == "error" and by_id["800200001"].jd_text.startswith(SEARCH["data"][0]["teaser"][:20])
    assert by_id["800200002"].company == "Northstar Devices"


def test_to_rawjob_company_fallbacks():
    row = {"id": "1", "title": "t", "companyName": "", "advertiser": {"description": "Adv Ltd"}, "locations": [], "listingDate": "", "teaser": "x", "bulletPoints": ["a", "b"]}
    rj = to_rawjob(row, {})
    assert rj.company == "Adv Ltd" and rj.jd_text == "x\na\nb" and rj.location == ""
    assert to_rawjob({"id": "2", "title": "t", "teaser": ""}, {}).company == "(待抽取)"
