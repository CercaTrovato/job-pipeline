from __future__ import annotations
import pathlib
import pytest
from jp.rules import hard

ROOT = pathlib.Path(__file__).resolve().parents[1]
C = hard.load_constraints(ROOT / "tests" / "fixtures" / "constraints.yaml")

def hc(**kw):
    base = dict(location=[], days_per_week=None, min_months=None, employment_type="unknown",
                graduated_required=False, onsite_days=None, visa="unknown", language=[])
    base.update(kw)
    return base

def test_cn_shenzhen_internship_passes():
    r = hard.evaluate(hc(location=["深圳"], days_per_week=4, min_months=3, employment_type="internship"), "CN", C)
    assert r.passed and r.reasons == []

def test_cn_other_city_fails():
    r = hard.evaluate(hc(location=["北京"]), "CN", C)
    assert not r.passed and any("地点" in x for x in r.reasons)

def test_cn_five_days_is_soft_flag_not_rejection():
    # Synthetic test scenario: five days per week is a soft flag.
    r = hard.evaluate(hc(location=["深圳"], days_per_week=5), "CN", C)
    assert r.passed and r.reasons == [] and r.flags == ["要求每周 5 天（可谈）"]
    assert hard.evaluate(hc(location=["深圳"], days_per_week=4), "CN", C).flags == []
    r2 = hard.evaluate(hc(location=["Hong Kong"], days_per_week=5), "HK", C)
    assert r2.passed and r2.flags == ["要求每周 5 天（可谈）"]

def test_regex_five_days_is_soft_flag():
    row = _row(title="算法实习生", jd_text="每周 5 天到岗，3 个月起", location="深圳")
    r = hard.check_job(row, C)
    assert r.passed and r.flags == ["要求每周 5 天（可谈）"]
    row2 = _row(title="Data Intern", jd_text="5 days per week, full-time only", location="Hong Kong", region="HK")
    r2 = hard.check_job(row2, C)
    assert r2.passed and r2.flags == ["要求每周 5 天 / 仅全职（可谈）"]

def test_remote_internship_may_be_in_chengdu():
    # Synthetic test scenario: remote work permits this otherwise excluded test city.
    assert not hard.check_job(_row(title="数据分析实习生", location="成都"), C).passed
    r = hard.check_job(_row(title="数据分析实习生（远程）", location="成都"), C)
    assert r.passed and "远程" in " ".join(r.flags)
    assert hard.check_job(_row(title="数据分析实习生", jd_text="支持远程实习，每周线上沟通", location="成都"), C).passed
    assert hard.check_job(_row(title="Data Intern", location="Chengdu", raw_json='{"workplace_type": "遠距"}', region="CN"), C).passed
    assert hard.check_job(_row(title="Data Intern", location="Chengdu", raw_json='{"work_arrangement": "Remote"}', region="CN"), C).passed
    assert not hard.check_job(_row(title="数据分析实习生（远程）", location="北京"), C).passed     # 远程也只放开成都
    assert hard.check_job(_row(title="数据分析实习生", location="成都"), C, analysis_hc=hc(location=["成都"]), analysis_flags={"remote": True}).passed

def test_cn_seven_months_fails_six_ok():
    assert hard.evaluate(hc(location=["深圳"], min_months=6), "CN", C).passed
    r = hard.evaluate(hc(location=["深圳"], min_months=7), "CN", C)
    assert not r.passed and r.reasons == ["要求 ≥7 个月"]

def test_hk_unknown_visa_is_allowed_in_synthetic_fixture():
    r = hard.evaluate(hc(location=["Hong Kong"], visa="unknown", min_months=6), "HK", C)
    assert r.passed

def test_hk_graduated_required_fails():
    r = hard.evaluate(hc(location=["Hong Kong"], graduated_required=True), "HK", C)
    assert not r.passed

def test_fulltime_fails_both_regions():
    assert not hard.evaluate(hc(location=["深圳"], employment_type="fulltime"), "CN", C).passed
    assert not hard.evaluate(hc(location=["Hong Kong"], employment_type="fulltime"), "HK", C).passed

def test_unknown_location_passes_with_no_reason():
    # 模型抽不出地点时不淘汰，交给人看
    assert hard.evaluate(hc(location=[]), "CN", C).passed

def test_regex_prescreen_cn():
    assert hard.regex_prescreen("岗位要求：每周 5 天到岗，实习 6 个月以上", "CN", C) == []          # 5 天改为软标注
    assert hard.regex_prescreen("实习生，每周 4 天，3 个月起", "CN", C) == []
    assert hard.regex_prescreen("实习 7 个月以上", "CN", C) == ["要求 ≥7 个月"]
    assert hard.regex_prescreen("能实习一年以上者优先", "CN", C) == ["要求 ≥7 个月"]
    assert hard.regex_prescreen("实习 12 个月起", "CN", C) == ["要求 ≥7 个月"]

def test_regex_prescreen_hk():
    assert "要求已毕业" in hard.regex_prescreen("Fresh graduates only", "HK", C)

def test_regex_fulltime_lookahead_is_sentence_scoped():
    assert hard.regex_prescreen("全职实习生，每周 4 天", "CN", C) == []
    assert hard.regex_prescreen("Full-time internship, 3 months", "CN", C) == []
    assert hard.regex_prescreen("岗位为全职岗位，工作强度较高。公司另设暑期实习生项目。", "CN", C) == ["要求全职"]
    assert hard.regex_prescreen("This is a full-time position.", "CN", C) == ["要求全职"]


# ---------- 轨道（实习 / 校招）与岗位级字段 ----------

def _row(**kw):
    base = dict(title="LLM 实习生", jd_text="", location="", region="CN", raw_json="{}")
    base.update(kw)
    return base

def test_detect_track_title_intern_wins():
    assert hard.detect_track("2026 校招实习生", "校招") == "intern"
    assert hard.detect_track("Software Engineer Intern", "campus hire") == "intern"

def test_detect_track_campus_by_title_recruit_type_or_jd():
    assert hard.detect_track("算法工程师（26届校招）", "") == "campus"
    assert hard.detect_track("算法工程师", "", recruit_type="校招") == "campus"
    assert hard.detect_track("Graduate Programme 2027", "") == "campus"
    assert hard.detect_track("大模型推理加速-27届星河秋招", "") == "campus"
    assert hard.detect_track("算法工程师", "面向 2027 届应届毕业生") == "campus"
    assert hard.detect_track("算法工程师", "", url="https://x.zhiye.com/campus/jobs#jobAdId=1") == "campus"
    assert hard.detect_track("算法工程师", "负责模型训练") == "intern"

def test_detect_track_internship_listing_fields_win_over_campus_words():
    # 牛客 / Boss 的实习帖带到岗天数：哪怕标题写"27届秋招"、JD 提"校招"，也是实习轨道
    assert hard.detect_track("大模型推理加速-27届星河秋招", "面向2027届", internship_fields=True) == "intern"
    assert hard.detect_track("AI-HR培训生（沟通方向）", "优秀者可获校招 offer 直通", internship_fields=True) == "intern"
    assert hard.detect_track("算法工程师", "表现优秀可获校招offer直通") == "intern"   # JD 弱提及不算校招
    row = _row(title="AI算法工程师-27届秋招", jd_text="面向2027届应届毕业生", location="深圳", url="https://www.nowcoder.com/jobs/detail/1",
               raw_json='{"days_per_week": 5, "min_months": 9}')
    r = hard.check_job(row, C)
    assert r.track == "intern" and r.reasons == ["要求 ≥9 个月"] and r.flags == ["要求每周 5 天（可谈）"]

def test_job_conditions_from_adapter_fields():
    row = _row(location="北京，上海", raw_json='{"recruit_type": "全职", "days_per_week": 5, "min_months": 3}')
    assert hard.job_conditions(row) == {"location": ["北京", "上海"], "days_per_week": 5, "min_months": 3, "employment_type": "fulltime"}
    assert hard.job_conditions(_row()) == {}
    assert hard.job_conditions(_row(raw_json='{"recruit_type": "实习"}')) == {"employment_type": "internship"}

def test_job_conditions_english_work_type_fields_guarded_by_title():
    # JobsDB work_types / Workday time_type / BambooHR employment / Pinpoint employment_type：标题没有 intern 等字样才当全职
    assert hard.job_conditions(_row(title="Data Engineer", raw_json='{"work_types": ["Full time"]}'))["employment_type"] == "fulltime"
    assert hard.job_conditions(_row(title="Data Engineer", raw_json='{"work_types": ["Contract/Temp"]}'))["employment_type"] == "fulltime"
    assert "employment_type" not in hard.job_conditions(_row(title="Data Intern", raw_json='{"work_types": ["Full time"]}'))
    assert "employment_type" not in hard.job_conditions(_row(title="Graduate Trainee", raw_json='{"work_types": ["Full time"]}'))
    assert hard.job_conditions(_row(title="Analyst", raw_json='{"work_types": ["Part time"]}'))["employment_type"] == "parttime"
    assert hard.job_conditions(_row(title="Analyst", raw_json='{"time_type": "Full time"}'))["employment_type"] == "fulltime"
    assert hard.job_conditions(_row(title="Analyst", raw_json='{"employment": "Full-Time"}'))["employment_type"] == "fulltime"
    assert hard.job_conditions(_row(title="Analyst", raw_json='{"employment_type": "permanent_full_time"}'))["employment_type"] == "fulltime"
    assert "employment_type" not in hard.job_conditions(_row(title="Analyst", raw_json='{"time_type": ""}'))

def test_merge_conditions_model_first_then_adapter():
    model = hc(location=[], days_per_week=None, employment_type="unknown")
    merged = hard.merge_conditions(model, {"location": ["北京"], "days_per_week": 5, "employment_type": "fulltime"})
    assert merged["location"] == ["北京"] and merged["days_per_week"] == 5 and merged["employment_type"] == "fulltime"
    model2 = hc(location=["深圳"], days_per_week=4, employment_type="internship")
    merged2 = hard.merge_conditions(model2, {"location": ["北京"], "days_per_week": 5, "employment_type": "fulltime"})
    assert merged2["location"] == ["深圳", "北京"] and merged2["days_per_week"] == 4 and merged2["employment_type"] == "internship"

def test_campus_track_accepts_chengdu_and_fulltime_cn():
    assert not hard.evaluate(hc(location=["成都"]), "CN", C).passed                       # 实习轨道：成都不行
    assert hard.evaluate(hc(location=["成都"], employment_type="fulltime"), "CN", C, track="campus").passed
    assert hard.evaluate(hc(location=["深圳"], days_per_week=5, min_months=12), "CN", C, track="campus").passed
    r = hard.evaluate(hc(location=["北京"]), "CN", C, track="campus")
    assert not r.passed and any("地点" in x for x in r.reasons)

def test_campus_track_hk_keeps_location_and_generic_graduation_rule():
    assert hard.evaluate(hc(location=["Hong Kong"], employment_type="fulltime"), "HK", C, track="campus").passed
    assert not hard.evaluate(hc(location=["新加坡"], employment_type="fulltime"), "HK", C, track="campus").passed
    assert "要求已毕业" in hard.regex_prescreen("Fresh graduates only", "HK", C, track="campus")
    assert hard.regex_prescreen("5 days per week, full-time only", "HK", C, track="campus") == []

def test_class_year_rule_campus_only():
    assert hard.class_year_reasons("算法工程师（26届校招）", [2027]) == ["届别不符: 2026（要求 2027 届）"]
    assert hard.class_year_reasons("2027届校园招聘", [2027]) == []
    assert hard.class_year_reasons("面向 2026 届及以后毕业生", [2027]) == []
    assert hard.class_year_reasons("Graduate Programme, Class of 2026", [2027]) != []
    assert hard.class_year_reasons("无届别信息", [2027]) == []
    assert hard.regex_prescreen("算法工程师（26届校招）", "CN", C, track="campus") == ["届别不符: 2026（要求 2027 届）"]
    assert hard.regex_prescreen("算法工程师（26届校招）", "CN", C, track="intern") == []

def test_structured_graduation_year_beats_text():
    # 牛客校招帖带 graduationYear：2026届 → 不符；2027届 / 毕业不限 → 通过；文本里的其它届别不再误判
    base = dict(title="AI算法工程师", location="深圳", jd_text="面向 2026 届及 2027 届", region="CN")
    ok = hard.check_job(_row(raw_json='{"recruit_type": "校招", "graduation_year": "2027届"}', **base), C)
    assert ok.passed and ok.track == "campus"
    assert hard.check_job(_row(raw_json='{"recruit_type": "校招", "graduation_year": "毕业不限"}', **base), C).passed
    bad = hard.check_job(_row(raw_json='{"recruit_type": "校招", "graduation_year": "2026届"}', **base), C)
    assert not bad.passed and bad.reasons == ["届别不符: 2026（要求 2027 届）"]
    assert hard.check_job(_row(raw_json='{"recruit_type": "校招"}', title="算法工程师", location="成都", jd_text="2027届校园招聘", region="CN"), C).passed


def test_hk_district_names_count_as_hong_kong():
    for loc in ("Kowloon Bay, Kwun Tong District", "Central & Western District, Hong Kong SAR (Hybrid)",
                "Causeway Bay, Wan Chai District", "HK-ONE ES 30/F", "Tsim Sha Tsui, Yau Tsim Mong District", "中国香港"):
        assert hard.evaluate(hc(location=[loc]), "HK", C).passed, loc
    for loc in ("新加坡", "北京", "Sydney", "Singapore"):
        assert not hard.evaluate(hc(location=[loc]), "HK", C).passed, loc

def test_check_job_feishu_fulltime_in_beijing_rejected_before_model():
    row = _row(title="MaaS 平台开发工程师", location="北京，上海", raw_json='{"recruit_type": "全职"}')
    r = hard.check_job(row, C)
    assert not r.passed and r.track == "intern"
    assert any("地点不符" in x for x in r.reasons) and any("雇佣类型不符" in x for x in r.reasons)

def test_check_job_merges_model_and_adapter():
    row = _row(title="算法工程师（27届校招）", location="成都", raw_json='{"recruit_type": "全职"}')
    r = hard.check_job(row, C, analysis_hc=hc(location=[], employment_type="unknown"))
    assert r.passed and r.track == "campus"
    row2 = _row(title="LLM 实习生", location="深圳", jd_text="每周 5 天到岗，6 个月以上", raw_json='{"days_per_week": 5, "min_months": 8}')
    r2 = hard.check_job(row2, C, analysis_hc=hc(location=["深圳"]))
    assert not r2.passed and r2.reasons == ["要求 ≥8 个月"] and r2.flags == ["要求每周 5 天（可谈）"]
