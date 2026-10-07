from __future__ import annotations
import json
import pathlib
import pytest
from jp.adapters.base import RateLimiter, SearchQuery
from jp.adapters.linkedin import LinkedInAdapter, job_id_from_url, to_rawjob

FIX = pathlib.Path(__file__).resolve().parent / "fixtures" / "adapters"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class FakeRun:
    def __init__(self):
        self.calls = []
    def __call__(self, args, **kw):
        self.calls.append(args)
        if args[:2] == ["linkedin", "search"]:
            return load("linkedin_search.json") if args[args.index("--start") + 1] == "0" else []
        if args[:2] == ["linkedin", "job-detail"]:
            return load("linkedin_detail.json") if "900100002" in args[2] else {}
        raise AssertionError(args)


def test_job_id_from_url():
    assert job_id_from_url("https://www.linkedin.com/jobs/view/900100002") == "900100002"
    assert job_id_from_url("https://www.linkedin.com/jobs/search/?currentJobId=900100002") == "900100002"
    assert job_id_from_url("") == ""


def test_search_and_detail_merge():
    run = FakeRun()
    q = SearchQuery(region="HK", keywords=["intern"], city="Hong Kong", max_pages=2, page_size=25,
                    extra={"experience-level": "internship", "date-posted": "month"})
    lim = RateLimiter(0, 0, sleep=lambda s: None)
    jobs = LinkedInAdapter(run=run).search(q, lim, log=lambda s: None)
    s = run.calls[0]
    assert s[2] == "intern" and s[s.index("--location") + 1] == "Hong Kong" and s[s.index("--limit") + 1] == "25"
    assert s[s.index("--experience-level") + 1] == "internship" and s[s.index("--date-posted") + 1] == "month"
    assert [a[1] for a in run.calls] == ["search", "job-detail", "job-detail"]   # 第一页不足 25 条 → 不翻第二页
    a, b = jobs
    assert a.platform_id == "900100001" and a.company == "Harbor Analytics" and a.jd_text == "" and a.raw["apply_url"] == ""
    assert b.platform_id == "900100002" and b.company == "Northstar Labs" and b.title == "Regional Analytics Engineering Intern"
    assert b.url == "https://www.linkedin.com/jobs/view/900100002/" and b.location == "Hong Kong SAR (Hybrid)" and b.posted_at == "2030-02-03"
    assert b.jd_text.startswith("Key Responsibilities") and "關於該職缺" not in b.jd_text
    assert b.raw["apply_url"].startswith("https://apply.example.test/") and b.raw["workplace_type"] == "現場"
    assert lim.calls == 3


def test_known_ids_skip_job_detail_but_still_returned():
    run = FakeRun()
    q = SearchQuery(region="HK", keywords=["intern"], city="Hong Kong", max_pages=1, page_size=25, detail_limit=1,
                    extra={"experience-level": "internship", "known_ids": {"900100001"}})
    jobs = LinkedInAdapter(run=run).search(q, RateLimiter(0, 0, sleep=lambda s: None), log=lambda s: None)
    assert [a[1] for a in run.calls] == ["search", "job-detail"]
    assert "900100002" in run.calls[1][2]                       # 预算给了未入库的那条
    by_id = {j.platform_id: j for j in jobs}
    assert by_id["900100001"].raw["detail_skipped"] == "known" and by_id["900100002"].company == "Northstar Labs"


def test_detail_failure_falls_back_to_list_row_without_aborting():
    from jp.adapters.base import AdapterError, AuthRequiredError
    run = FakeRun()
    orig = run.__call__

    class Flaky:
        def __init__(self):
            self.calls = []
        def __call__(self, args, **kw):
            self.calls.append(args)
            if args[:2] == ["linkedin", "job-detail"] and "900100001" in args[2]:
                raise AdapterError("opencli linkedin job-detail: 无输出（LinkedIn job detail could not find a job title）")
            return orig(args, **kw)

    flaky = Flaky()
    q = SearchQuery(region="HK", keywords=["intern"], city="Hong Kong", max_pages=1, page_size=25, detail_limit=5)
    jobs = LinkedInAdapter(run=flaky).search(q, RateLimiter(0, 0, sleep=lambda s: None), log=lambda s: None)
    by_id = {j.platform_id: j for j in jobs}
    assert by_id["900100001"].raw["detail_skipped"] == "error" and by_id["900100001"].company == "Harbor Analytics"
    assert by_id["900100002"].company == "Northstar Labs"                       # 其它岗位照常拿到详情

    class NotLoggedIn(Flaky):
        def __call__(self, args, **kw):
            if args[:2] == ["linkedin", "job-detail"]:
                raise AuthRequiredError("linkedin 未登录")
            return orig(args, **kw)
    import pytest
    with pytest.raises(AuthRequiredError):                                # 登录 / 风控错误仍然向上抛
        LinkedInAdapter(run=NotLoggedIn()).search(q, RateLimiter(0, 0, sleep=lambda s: None), log=lambda s: None)


def test_to_rawjob_strips_prefix_variants():
    row = {"title": "t", "company": "C", "location": "HK", "listed": "2026-09-01", "salary": "", "url": "https://www.linkedin.com/jobs/view/1"}
    assert to_rawjob(row, {"description": "About the job\nDo things"}).jd_text == "Do things"
    assert to_rawjob(row, {"description": "关于该职位 Do"}).jd_text == "Do"


def test_patch_text_states():
    helper = pytest.importorskip("tools.opencli_patch_linkedin")
    OLD, NEW, patch_text = helper.OLD, helper.NEW, helper.patch_text
    src = "x\n        " + OLD + "\ny"
    new, state = patch_text(src)
    assert state == "patched" and NEW in new and OLD not in new
    assert patch_text(new)[1] == "already"
    assert patch_text("nothing")[1] == "missing"
