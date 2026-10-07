from __future__ import annotations
import json
import pytest
from jp import fetch as jf, sentinel
from jp.adapters.base import AdapterError, AuthRequiredError, RateLimiter, RiskControlError, SearchQuery
from jp.models import RawJob

CFG = {
    "fetch": {
        "rate": {"boss": [5, 10], "linkedin": [6, 12], "jobsdb": [3, 6], "nowcoder": [3, 6], "watchlist": [2, 4]},
        "cooldown_hours": 24,
        "CN": {"boss": {"keywords": ["大模型 实习"], "city": "深圳", "max_pages": 2, "page_size": 15, "detail_limit": 5,
                        "extra": {"experience": "在校生", "job_type": "实习"}},
               "nowcoder": {"keywords": ["大模型"], "city": "深圳"}},
        "HK": {"linkedin": {"keywords": ["intern"], "city": "Hong Kong", "extra": {"experience-level": "internship"}},
               "jobsdb": {"keywords": ["intern"]}},
        "watchlist": {"max_pages": 3, "detail_limit": 30},
    }
}


def _raw(i):
    return RawJob(platform_id="p%d" % i, title="LLM 实习 %d" % i, company="公司%d" % i, url="https://x/%d" % i, jd_text="jd", location="深圳")


class Stub:
    def __init__(self, source, result=None, exc=None):
        self.source, self.result, self.exc, self.calls = source, result or [], exc, []
    def search(self, q, limiter, log=print):
        self.calls.append((q, limiter))
        if self.exc:
            raise self.exc
        return self.result


def test_build_query_and_limiter():
    q = jf.build_query(CFG, "CN", "boss")
    assert isinstance(q, SearchQuery) and q.keywords == ["大模型 实习"] and q.city == "深圳" and q.max_pages == 2 and q.detail_limit == 5
    assert q.extra == {"experience": "在校生", "job_type": "实习"}
    q2 = jf.build_query(CFG, "CN", "nowcoder")
    assert (q2.max_pages, q2.page_size, q2.detail_limit, q2.extra) == (3, 15, 40, {})
    lim = jf.make_limiter(CFG, "boss")
    assert isinstance(lim, RateLimiter) and (lim.min_s, lim.max_s) == (5.0, 10.0)
    assert (jf.make_limiter(CFG, "unknown").min_s, jf.make_limiter(CFG, "unknown").max_s) == (3.0, 6.0)


def test_run_fetch_ingests_and_records_run(conn):
    stubs = {"boss": Stub("boss", [_raw(1), _raw(2)]), "nowcoder": Stub("nowcoder", [_raw(2), _raw(3)])}
    reps = jf.run_fetch(CFG, conn, "CN", ["boss", "nowcoder"], root=None, log=lambda s: None,
                        adapter_factory=lambda s: stubs[s], browser_check=lambda: (True, "ok"))
    assert [(r.source, r.status, r.new, r.seen, r.merged) for r in reps] == [("boss", "done", 2, 0, 0), ("nowcoder", "done", 1, 0, 1)]
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    runs = conn.execute("SELECT command, source, status, jobs_new FROM runs ORDER BY run_id").fetchall()
    assert [tuple(r) for r in runs] == [("fetch", "boss", "done", 2), ("fetch", "nowcoder", "done", 1)]
    assert stubs["boss"].calls[0][0].keywords == ["大模型 实习"]


def test_browser_not_ready_skips_browser_sources_only(conn):
    stubs = {"linkedin": Stub("linkedin", [_raw(1)]), "jobsdb": Stub("jobsdb", [_raw(2)])}
    logs = []
    reps = jf.run_fetch(CFG, conn, "HK", ["linkedin", "jobsdb"], root=None, log=logs.append,
                        adapter_factory=lambda s: stubs[s], browser_check=lambda: (False, "[MISSING] Extension: not connected"))
    assert [(r.source, r.status) for r in reps] == [("linkedin", "skipped"), ("jobsdb", "done")]
    assert stubs["linkedin"].calls == [] and any("Edge" in s for s in logs)


def test_risk_control_starts_cooldown_and_next_run_is_skipped(conn):
    stub = Stub("boss", exc=RiskControlError("opencli boss search: 疑似风控（请完成安全验证）"))
    reps = jf.run_fetch(CFG, conn, "CN", ["boss"], root=None, log=lambda s: None, adapter_factory=lambda s: stub, browser_check=lambda: (True, ""))
    assert reps[0].status == "cooldown" and "安全验证" in reps[0].error
    assert sentinel.is_cooling(conn, "boss") is not None
    assert conn.execute("SELECT status, last_error FROM runs").fetchone()["status"] == "cooldown"
    stub2 = Stub("boss", [_raw(1)])
    reps2 = jf.run_fetch(CFG, conn, "CN", ["boss"], root=None, log=lambda s: None, adapter_factory=lambda s: stub2, browser_check=lambda: (True, ""))
    assert reps2[0].status == "cooldown" and stub2.calls == []
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1     # 冷却中不再记 run


def test_auth_and_adapter_errors_are_reported_not_raised(conn):
    stubs = {"boss": Stub("boss", exc=AuthRequiredError("未登录")), "nowcoder": Stub("nowcoder", exc=AdapterError("解析失败"))}
    reps = jf.run_fetch(CFG, conn, "CN", ["boss", "nowcoder"], root=None, log=lambda s: None, adapter_factory=lambda s: stubs[s], browser_check=lambda: (True, ""))
    assert [(r.source, r.status) for r in reps] == [("boss", "auth_required"), ("nowcoder", "error")]
    assert [r["status"] for r in conn.execute("SELECT status FROM runs ORDER BY run_id")] == ["error", "error"]
    assert sentinel.is_cooling(conn, "boss") is None


def test_one_failing_query_block_keeps_the_others(conn):
    """多查询块的来源（Boss 深圳实习 + 深圳校招 + 成都校招）里某一块失败（会话卡住之类），
    前面几块已经抓到的仍然入库，状态记 partial 并带上失败原因。"""
    cfg = json.loads(json.dumps(CFG))
    cfg["fetch"]["CN"]["boss"] = [dict(cfg["fetch"]["CN"]["boss"]), dict(cfg["fetch"]["CN"]["boss"], city="成都")]

    class Flaky:
        def __init__(self):
            self.calls = 0
        def search(self, q, limiter, log=print):
            self.calls += 1
            if self.calls == 2:
                raise AdapterError("boss 页面未就绪: about:blank")
            return [_raw(1)]

    stub = Flaky()
    reps = jf.run_fetch(cfg, conn, "CN", ["boss"], root=None, log=lambda s: None,
                        adapter_factory=lambda s: stub, browser_check=lambda: (True, ""))
    assert reps[0].status == "partial" and reps[0].new == 1 and "页面未就绪" in reps[0].error
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_all_blocks_failing_still_reports_error(conn):
    stub = Stub("boss", exc=AdapterError("boss 页面未就绪: about:blank"))
    reps = jf.run_fetch(CFG, conn, "CN", ["boss"], root=None, log=lambda s: None,
                        adapter_factory=lambda s: stub, browser_check=lambda: (True, ""))
    assert reps[0].status == "error" and reps[0].new == 0


def test_watchlist_uses_injected_runner_and_ticks(conn):
    seen = []
    def runner(cfg, region, root, limiter, log, on_company):
        on_company(1, 2); on_company(2, 2)
        seen.append((region, limiter.min_s))
        return [_raw(9)]
    reps = jf.run_fetch(CFG, conn, "HK", ["watchlist"], root=None, log=lambda s: None, watchlist_runner=runner, browser_check=lambda: (True, ""))
    assert reps[0].status == "done" and reps[0].new == 1 and seen == [("HK", 2.0)]
    row = conn.execute("SELECT pages_done, pages_total FROM runs").fetchone()
    assert (row["pages_done"], row["pages_total"]) == (2, 2)


def test_source_not_configured_for_region_is_skipped_not_raised(conn):
    stubs = {"linkedin": Stub("linkedin", [_raw(1)]), "boss": Stub("boss", [_raw(2)])}
    reps = jf.run_fetch(CFG, conn, "CN", ["linkedin", "boss"], root=None, log=lambda s: None,
                        adapter_factory=lambda s: stubs[s], browser_check=lambda: (True, ""))
    assert [(r.source, r.status) for r in reps] == [("linkedin", "skipped"), ("boss", "done")]
    assert stubs["linkedin"].calls == [] and stubs["boss"].calls != []
    assert [r["source"] for r in conn.execute("SELECT source FROM runs")] == ["boss"]


def test_run_fetch_passes_known_ids_and_hard_limits_to_adapter(conn):
    """增量抓取：把该来源已入库的 platform_id 与 constraints 里的天数 / 月数上限塞进 q.extra，适配器据此省详情预算。"""
    from jp import ingest
    ingest.ingest_jobs(conn, [_raw(1)], source="boss", region="CN")
    ingest.ingest_jobs(conn, [_raw(9)], source="nowcoder", region="CN")
    stub = Stub("boss", [_raw(2)])
    constraints = {"CN": {"max_days_per_week": 4, "max_min_months": 6}, "HK": {"max_days_per_week": 4, "max_min_months": 99}}
    jf.run_fetch(CFG, conn, "CN", ["boss"], root=None, log=lambda s: None, adapter_factory=lambda s: stub,
                 browser_check=lambda: (True, ""), constraints=constraints)
    q = stub.calls[0][0]
    assert q.extra["known_ids"] == {"p1"}                       # 只含本来源
    conn.execute("UPDATE jobs SET raw_json=? WHERE platform_id='p1'", ('{"detail_skipped": "hard_prefilter"}',))
    conn.commit()
    assert jf.known_platform_ids(conn, "boss") == {"p2"}         # 只有列表字段的 p1 不算已知，下次要补详情
    conn.execute("UPDATE jobs SET raw_json=? WHERE platform_id='p1'", ('{"detail_skipped": "error", "detail_attempts": 2}',))
    conn.commit()
    assert jf.known_platform_ids(conn, "boss") == {"p1", "p2"}   # 重试两次仍失败的行不再占详情预算
    assert q.extra["max_days_per_week"] == 4 and q.extra["max_min_months"] == 6
    assert q.extra["experience"] == "在校生"                     # 配置里的 extra 保留
    assert jf.build_query(CFG, "CN", "boss").extra == {"experience": "在校生", "job_type": "实习"}   # 纯配置查询不带运行时键
    stub2 = Stub("boss", [])
    jf.run_fetch(CFG, conn, "CN", ["boss"], root=None, log=lambda s: None, adapter_factory=lambda s: stub2,
                 browser_check=lambda: (True, ""))
    assert stub2.calls[0][0].extra["known_ids"] == {"p1", "p2"} and "max_days_per_week" not in stub2.calls[0][0].extra   # p1 重试次数已用完 → 视为已知
    stub3 = Stub("boss", [])
    jf.run_fetch(CFG, conn, "CN", ["boss"], root=None, log=lambda s: None, adapter_factory=lambda s: stub3,
                 browser_check=lambda: (True, ""), constraints={"CN": {"max_days_per_week": 4, "days_negotiable": True, "max_min_months": 6}})
    assert "max_days_per_week" not in stub3.calls[0][0].extra and stub3.calls[0][0].extra["max_min_months"] == 6   # 天数可谈：不预筛


CFG_BLOCKS = {
    "fetch": {
        "rate": dict(CFG["fetch"]["rate"]), "cooldown_hours": 24,
        "CN": {"nowcoder": [
            {"keywords": ["大模型"], "city": "深圳"},
            {"keywords": ["算法"], "city": "成都", "extra": {"recruit_type": "1"}, "track": "campus"},
        ]},
        "HK": CFG["fetch"]["HK"], "watchlist": CFG["fetch"]["watchlist"],
    }
}


def test_source_config_may_be_a_list_of_query_blocks(conn):
    """同一来源可配多个查询块（实习块 + 校招块 / 不同城市）；校招块抓到的岗位标 recruit_type=校招，预筛上限按校招口径。"""
    qs = jf.build_queries(CFG_BLOCKS, "CN", "nowcoder")
    assert [q.city for q in qs] == ["深圳", "成都"] and qs[1].extra == {"recruit_type": "1"}
    assert jf.build_queries(CFG, "CN", "boss")[0].keywords == ["大模型 实习"]      # 单块写法仍可用
    calls = []

    class Stub2:
        def search(self, q, limiter, log=print):
            calls.append(q)
            return [RawJob(platform_id="p-" + q.city, title="算法工程师", company="C", url="u", jd_text="jd", location=q.city)]

    constraints = {"CN": {"max_days_per_week": 4, "max_min_months": 6, "campus": {"max_days_per_week": 7, "max_min_months": 99}}}
    reps = jf.run_fetch(CFG_BLOCKS, conn, "CN", ["nowcoder"], root=None, log=lambda s: None, adapter_factory=lambda s: Stub2(),
                        browser_check=lambda: (True, ""), constraints=constraints)
    assert [(r.source, r.status, r.new) for r in reps] == [("nowcoder", "done", 2)]
    assert (calls[0].extra["max_days_per_week"], calls[0].extra["max_min_months"]) == (4, 6)
    assert (calls[1].extra["max_days_per_week"], calls[1].extra["max_min_months"]) == (7, 99)
    import json
    rows = {r["location"]: json.loads(r["raw_json"]) for r in conn.execute("SELECT location, raw_json FROM jobs")}
    assert rows["成都"]["recruit_type"] == "校招" and "recruit_type" not in rows["深圳"]
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE source='nowcoder'").fetchone()[0] == 1


def _cfg_with_percent(where, value):
    """CFG 的独立副本，把 fetch.CN.boss 的 keywords/city/extra 之一改成含 '%' 的值。"""
    cfg = {"fetch": {"rate": dict(CFG["fetch"]["rate"]), "cooldown_hours": 24,
                     "CN": {"boss": {"keywords": ["大模型 实习"], "city": "深圳", "max_pages": 2, "page_size": 15,
                                     "detail_limit": 5, "extra": {"experience": "在校生", "job_type": "实习"}},
                            "nowcoder": {"keywords": ["大模型"], "city": "深圳"}},
                     "HK": CFG["fetch"]["HK"], "watchlist": CFG["fetch"]["watchlist"]}}
    if where == "keywords":
        cfg["fetch"]["CN"]["boss"]["keywords"] = [value]
    elif where == "city":
        cfg["fetch"]["CN"]["boss"]["city"] = value
    else:
        cfg["fetch"]["CN"]["boss"]["extra"][where] = value
    return cfg


def test_build_query_rejects_percent_in_keyword():
    cfg = _cfg_with_percent("keywords", "50% 远程")
    with pytest.raises(ValueError, match=r"fetch\.CN\.boss"):
        jf.build_query(cfg, "CN", "boss")


def test_build_query_rejects_percent_in_city_and_extra():
    with pytest.raises(ValueError, match=r"fetch\.CN\.boss"):
        jf.build_query(_cfg_with_percent("city", "深圳%"), "CN", "boss")
    with pytest.raises(ValueError, match=r"fetch\.CN\.boss"):
        jf.build_query(_cfg_with_percent("job_type", "全职%实习"), "CN", "boss")


def test_run_fetch_reports_error_and_skips_adapter_when_config_has_percent(conn):
    cfg = _cfg_with_percent("keywords", "50% 远程")
    stub = Stub("boss", [_raw(1)])
    reps = jf.run_fetch(cfg, conn, "CN", ["boss"], root=None, log=lambda s: None,
                        adapter_factory=lambda s: stub, browser_check=lambda: (True, ""))
    assert reps[0].source == "boss" and reps[0].status == "error" and "fetch.CN.boss" in reps[0].error
    assert stub.calls == []
    assert conn.execute("SELECT status FROM runs").fetchone()["status"] == "error"
