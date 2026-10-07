from __future__ import annotations
import pytest
from jp.adapters.base import (RateLimiter, SearchQuery, AdapterError, AuthRequiredError, RiskControlError,
                              html_to_text, keyword_hit, looks_like_risk, ms_to_date, split_for_detail)
from jp.adapters import REGION_SOURCES, BROWSER_SOURCES, get_adapter


def test_split_for_detail_known_first_then_budget():
    rows = {"a": 1, "b": 2, "c": 3, "d": 4}
    fetch, known, over = split_for_detail(rows, detail_limit=2, known_ids={"b"})
    assert fetch == [("a", 1), ("c", 3)] and known == [("b", 2)] and over == 1     # d 超预算留待下次
    fetch, known, over = split_for_detail(rows, detail_limit=10, known_ids=None)
    assert [k for k, _ in fetch] == ["a", "b", "c", "d"] and known == [] and over == 0


def test_rate_limiter_skips_first_call_then_sleeps_in_range():
    slept = []
    rl = RateLimiter(2, 4, sleep=slept.append, rand=lambda a, b: (a + b) / 2)
    rl.wait(); rl.wait(); rl.wait()
    assert slept == [3.0, 3.0]
    assert rl.calls == 3


def test_error_hierarchy():
    assert issubclass(AuthRequiredError, AdapterError)
    assert issubclass(RiskControlError, AdapterError)


def test_looks_like_risk():
    assert looks_like_risk("请完成安全验证后继续")
    assert looks_like_risk("We detected unusual activity")
    assert not looks_like_risk("豆包大模型评测实习生 500-550元/天")
    assert not looks_like_risk("please verify your email")   # "your" 不是 "you"，不该被 "verify you" 误命中
    assert looks_like_risk("verify you are human")


def test_html_to_text_blocks_and_entities():
    html = "<div><p>Key &amp; Responsibilities</p><ul><li>SQL</li><li>dbt&nbsp;models</li></ul><br>end</div>"
    assert html_to_text(html) == "Key & Responsibilities\nSQL\ndbt models\n\nend"   # 连续空行压成一行
    assert html_to_text("") == ""


def test_keyword_hit_case_insensitive_and_empty():
    assert keyword_hit("Data Science Intern", ["intern"])
    assert not keyword_hit("Senior Analyst", ["intern", "实习"])
    assert keyword_hit("anything", [])
    assert keyword_hit("大模型算法实习生", ["实习"])


def test_keyword_hit_short_ascii_keywords_need_word_boundary():
    assert keyword_hit("AI Engineer", ["AI"]) and keyword_hit("Applied ai/ml intern", ["AI"])
    assert not keyword_hit("Maintenance Engineer", ["AI"])       # 'ai' 是 Maintenance 的子串，不算命中
    assert not keyword_hit("Senior Analyst", ["AI"])


def test_ms_to_date():
    assert ms_to_date(1762932974906) == "2025-11-12"
    assert ms_to_date(None) == ""


def test_search_query_defaults():
    q = SearchQuery(region="CN", keywords=["大模型 实习"])
    assert (q.city, q.max_pages, q.page_size, q.detail_limit, q.extra) == ("", 3, 15, 40, {})


def test_registry():
    assert REGION_SOURCES["CN"] == ["boss", "nowcoder", "watchlist"]
    assert REGION_SOURCES["HK"] == ["linkedin", "jobsdb", "watchlist"]
    assert BROWSER_SOURCES == {"boss", "nowcoder", "linkedin"}
    with pytest.raises(KeyError):
        get_adapter("nope")
