from __future__ import annotations
import pytest
import yaml
from jp.adapters import watchlist as wl
from jp.adapters.base import AdapterError, RateLimiter, RiskControlError
from jp.models import RawJob

DOC = {
    "keywords_default": {"CN": ["实习"], "HK": ["Intern"]},
    "watchlist": [
        {"company": "甲", "region": "CN", "tier": "大厂", "grade": "A", "careers_url": "https://a", "ats": "fake", "keywords": ["实习"], "enabled": True, "params": {"x": 1}},
        {"company": "乙", "region": "CN", "tier": "小厂", "grade": "B", "careers_url": "https://b", "ats": "generic", "keywords": ["实习"], "enabled": True},
        {"company": "丙", "region": "CN", "tier": "小厂", "grade": "B", "careers_url": "https://c", "ats": "fake", "keywords": ["实习"], "enabled": False},
        {"company": "丁", "region": "HK", "tier": "大厂", "grade": "A", "careers_url": "https://d", "ats": "fake", "keywords": ["Intern"], "enabled": True},
    ],
}


def _raw(i, title):
    return RawJob(platform_id="fake:%d" % i, title=title, company="甲", url="https://a/%d" % i, jd_text="jd")


def test_load_and_split_entries(tmp_path):
    p = tmp_path / "w.yaml"
    p.write_text(yaml.safe_dump(DOC, allow_unicode=True), encoding="utf-8")
    doc = wl.load_watchlist(p)
    assert [e.company for e in wl.entries(doc, "CN")] == ["甲", "乙"]
    assert [e.company for e in wl.api_entries(doc, "CN", fetchers={"fake": None})] == ["甲"]
    assert [e.company for e in wl.generic_entries(doc, "CN", fetchers={"fake": None})] == ["乙"]
    e = wl.entries(doc, "CN")[0]
    assert e.params == {"x": 1} and e.keywords == ["实习"] and e.grade == "A"
    assert wl.entries(doc, "CN")[1].params == {}


def test_run_watchlist_filters_keywords_tags_raw_and_ticks(tmp_path):
    p = tmp_path / "w.yaml"
    p.write_text(yaml.safe_dump(DOC, allow_unicode=True), encoding="utf-8")
    cfg = {"paths": {"watchlist": str(p)}, "fetch": {"watchlist": {"max_pages": 2, "detail_limit": 5}}}
    calls, ticks = [], []
    def fake(entry, http, limits, limiter, log):
        calls.append((entry.company, limits))
        return [_raw(1, "大模型实习生"), _raw(2, "高级工程师")]
    jobs = wl.run_watchlist(cfg, "CN", None, RateLimiter(0, 0, sleep=lambda s: None), log=lambda s: None,
                            on_company=lambda d, t: ticks.append((d, t)), http=object(), fetchers={"fake": fake})
    assert calls == [("甲", {"max_pages": 2, "detail_limit": 5})] and ticks == [(1, 1)]
    assert [j.title for j in jobs] == ["大模型实习生"]
    assert jobs[0].raw["watchlist"] == {"company": "甲", "ats": "fake", "tier": "大厂", "grade": "A"}


def test_run_watchlist_company_error_is_logged_and_skipped_but_risk_propagates(tmp_path):
    p = tmp_path / "w.yaml"
    p.write_text(yaml.safe_dump(DOC, allow_unicode=True), encoding="utf-8")
    cfg = {"paths": {"watchlist": str(p)}, "fetch": {"watchlist": {}}}
    logs = []
    def boom(entry, http, limits, limiter, log):
        raise AdapterError("HTTP 500")
    assert wl.run_watchlist(cfg, "CN", None, RateLimiter(0, 0, sleep=lambda s: None), log=logs.append, http=object(), fetchers={"fake": boom}) == []
    assert any("甲" in s and "HTTP 500" in s for s in logs)
    def risk(entry, http, limits, limiter, log):
        raise RiskControlError("HTTP 429")
    with pytest.raises(RiskControlError):
        wl.run_watchlist(cfg, "CN", None, RateLimiter(0, 0, sleep=lambda s: None), log=logs.append, http=object(), fetchers={"fake": risk})


def test_root_joins_relative_watchlist_path(tmp_path):
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "w.yaml").write_text(yaml.safe_dump(DOC, allow_unicode=True), encoding="utf-8")
    cfg = {"paths": {"watchlist": "profile/w.yaml"}, "fetch": {"watchlist": {}}}
    jobs = wl.run_watchlist(cfg, "HK", tmp_path, RateLimiter(0, 0, sleep=lambda s: None), log=lambda s: None, http=object(),
                            fetchers={"fake": lambda e, h, l, lim, log: [RawJob(platform_id="fake:9", title="Data Intern", company="丁", url="https://d/9", jd_text="")]})
    assert len(jobs) == 1 and jobs[0].raw["watchlist"]["company"] == "丁"


def test_region_re():
    assert wl.REGION_RE["HK"].search("HK-TWO ES 8/F") and wl.REGION_RE["HK"].search("Hong Kong SAR") and not wl.REGION_RE["HK"].search("Bangkok")
    assert wl.REGION_RE["CN"].search("Shenzhen, China") and wl.REGION_RE["CN"].search("广东省·深圳市")


def test_run_watchlist_bad_entry_config_is_logged_and_skipped_but_on_company_still_called(tmp_path):
    """Task 9 review 补充：params 缺 tenant/sub/host 等会让抓取器抛 KeyError/TypeError/ValueError，
    这类配置错误应记日志跳过（不像未知异常那样让整个 region 中断），且仍要 tick on_company。"""
    p = tmp_path / "w.yaml"
    p.write_text(yaml.safe_dump(DOC, allow_unicode=True), encoding="utf-8")
    cfg = {"paths": {"watchlist": str(p)}, "fetch": {"watchlist": {}}}
    logs, ticks = [], []
    def bad_config(entry, http, limits, limiter, log):
        raise KeyError("tenant")
    jobs = wl.run_watchlist(cfg, "CN", None, RateLimiter(0, 0, sleep=lambda s: None), log=logs.append,
                            on_company=lambda d, t: ticks.append((d, t)), http=object(), fetchers={"fake": bad_config})
    assert jobs == []
    assert any("甲" in s and "配置错误" in s for s in logs)
    assert ticks == [(1, 1)]


def test_plan_lines_lists_generic_companies_with_hint():
    doc = {"keywords_default": {"CN": ["实习"]},
           "watchlist": [{"company": "乙", "region": "CN", "tier": "小厂", "grade": "B", "careers_url": "https://b", "ats": "generic", "keywords": ["实习", "AI"], "enabled": True, "note": "SPA"},
                         {"company": "甲", "region": "CN", "tier": "大厂", "grade": "A", "careers_url": "https://a", "ats": "workday", "keywords": ["实习"], "enabled": True, "params": {"tenant": "a", "site": "s"}}]}
    lines = wl.plan_lines(doc, "CN")
    assert lines[0].startswith("agent 亲自浏览清单（CN）")
    assert any("乙" in ln and "https://b" in ln and "实习, AI" in ln and "SPA" in ln for ln in lines)
    assert not any("甲" in ln for ln in lines)
    assert any("pipeline.py ingest --source watchlist --region CN --file" in ln for ln in lines)
