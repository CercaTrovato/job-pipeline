import sqlite3

from jp.selection_duplicates import (
    brand_key,
    compare_rows,
    find_approved_conflicts,
    normalized_jd,
)


def _row(job_id, company, title="AI 应用开发实习", jd=None, location="深圳", **extra):
    row = {
        "job_id": job_id,
        "source": extra.pop("source", "test"),
        "region": extra.pop("region", "CN"),
        "company": company,
        "title": title,
        "location": location,
        "url": extra.pop("url", ""),
        "jd_text": jd if jd is not None else ("负责 AI 应用开发与落地。" * 25),
        "raw_json": extra.pop("raw_json", "{}"),
    }
    row.update(extra)
    return row


def test_brand_key_unifies_legal_entities_and_keeps_similar_brands_separate():
    assert brand_key("华为技术有限公司") == brand_key("上海华为软件技术有限公司")
    assert brand_key("京东科技集团") != brand_key("京东方科技集团")
    assert brand_key("拼多多（PDD）") != brand_key("Temu")
    assert brand_key("小红书科技有限公司") == brand_key("行吟信息科技（上海）有限公司")
    assert brand_key("Xiaohongshu Inc.") == brand_key("REDnote Technology")
    assert brand_key("云有限公司") == "云"


def test_same_brand_exact_jd_detects_kuaishou_even_with_different_title():
    jd = "负责推荐算法和大模型应用开发，参与业务系统设计、编码、测试与上线维护。" * 8
    a = _row("ks-a", "北京快手科技有限公司", title="算法实习生", jd=jd)
    b = _row("ks-b", "快手科技", title="AI 应用开发实习生", jd="\n".join(jd.split()))
    result = compare_rows(a, b)
    assert result["kind"] == "exact"
    assert result["related_job_id"] == "ks-b"
    assert normalized_jd(jd) == normalized_jd("\n".join(jd.split()))


def test_huawei_chengdu_near_match_and_binance_same_title_near_match():
    base = "负责大模型应用研发，使用 Python 构建服务并完成测试和部署。" * 10
    changed = base[:-2] + "优化。"
    h1 = _row("hw-a", "华为技术有限公司", "AI 工程师", base, "成都")
    h2 = _row("hw-b", "成都华为技术有限公司", "AI 工程师", changed, "成都")
    b1 = _row("bi-a", "Binance Holdings", "Data Analyst Intern", base)
    b2 = _row("bi-b", "Binance Technology", "Data Analyst Intern", changed)
    assert compare_rows(h1, h2)["kind"] == "near"
    assert compare_rows(b1, b2)["kind"] == "near"


def test_same_official_ids_are_exact_and_other_brands_are_not_conflicts():
    a = _row("mt-a", "美团", url="https://zhaopin.meituan.com/web/position/detail?jobUnionId=12345")
    b = _row("mt-b", "美团金融", url="https://zhaopin.meituan.com/web/position/detail?jobUnionId=12345&x=1")
    assert compare_rows(a, b)["kind"] == "exact"
    same_jd_other_brand = _row("other", "哔哩哔哩", jd=a["jd_text"])
    assert compare_rows(a, same_jd_other_brand) is None


def test_platform_ids_are_recognized_and_region_city_track_gate_applies():
    a = _row("boss-a", "京东", url="https://www.zhipin.com/job_detail/AbC123.html")
    b = _row("boss-b", "京东科技", url="https://www.zhipin.com/job_detail/abc123.html")
    assert compare_rows(a, b)["kind"] == "exact"
    assert compare_rows(a, _row("boe", "京东方", jd=a["jd_text"])) is None
    assert compare_rows(a, _row("other-city", "京东", location="成都", jd=a["jd_text"])) is None
    campus = _row("campus", "京东", title="AI 应用开发校招", jd=a["jd_text"])
    assert compare_rows(a, campus) is None


def test_platform_detail_ids_still_require_same_city_and_track():
    nowcoder_a = _row("nc-a", "小红书", url="https://www.nowcoder.com/jobs/detail/415039")
    nowcoder_b = _row("nc-b", "小红书", url="https://www.nowcoder.com/jobs/detail/415039?from=search", location="成都")
    linkedin_a = _row("li-a", "Binance", url="https://www.linkedin.com/jobs/view/4463854244")
    linkedin_b = _row("li-b", "Binance Technology", url="https://www.linkedin.com/jobs/view/4463854244/")
    assert compare_rows(nowcoder_a, nowcoder_b) is None
    assert compare_rows(nowcoder_a, _row("nc-c", "小红书", title="AI 应用开发校招",
                                         url=nowcoder_a["url"])) is None
    match = compare_rows(linkedin_a, linkedin_b)
    assert match["kind"] == "exact" and "平台岗位编号" in match["reason"]


def test_multi_city_locations_match_by_intersection_and_missing_city_needs_exactness():
    jd = "负责数据平台研发，参与需求分析、方案设计、开发测试和线上问题排查。" * 8
    multi = _row("multi", "快手", title="数据开发实习", jd=jd, location="成都，深圳")
    shenzhen = _row("sz", "快手", title="数据开发实习", jd=jd[:-2] + "优化。", location="深圳市")
    assert compare_rows(multi, shenzhen)["kind"] == "near"

    missing_exact = _row("missing-exact", "快手", title="数据开发实习", jd=jd, location="")
    missing_similar = _row("missing-near", "快手", title="数据开发实习", jd=jd[:-2] + "优化。", location="")
    assert compare_rows(missing_exact, _row("missing-exact-2", "快手", title="其他职位", jd=jd, location=""))["kind"] == "exact"
    assert compare_rows(missing_exact, missing_similar) is None


def test_near_match_requires_both_jds_to_have_at_least_150_characters():
    a = _row("short-a", "Binance", "Data Intern", "A" * 140, "深圳")
    b = _row("short-b", "Binance", "Data Intern", "A" * 139 + "B", "深圳")
    assert compare_rows(a, b) is None


def test_find_approved_conflicts_is_read_only_and_returns_required_fields():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE jobs (job_id TEXT, source TEXT, region TEXT, platform_id TEXT, company TEXT, "
        "title TEXT, location TEXT, url TEXT, jd_text TEXT, raw_json TEXT, status TEXT)"
    )
    approved = _row("approved-1", "北京快手科技有限公司")
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (approved["job_id"], approved["source"], approved["region"], "", approved["company"],
         approved["title"], approved["location"], approved["url"], approved["jd_text"],
         approved["raw_json"], "approved"),
    )
    conn.commit()
    before = conn.total_changes
    incoming = _row("incoming", "快手")
    result = find_approved_conflicts(conn, incoming)
    assert conn.total_changes == before
    assert result == [{
        "related_job_id": "approved-1",
        "kind": "exact",
        "similarity": 1.0,
        "reason": "同品牌、同地区、同轨道，完整 JD 去空白后完全一致",
    }]
