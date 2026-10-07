from __future__ import annotations
from jp import normalize as n

def test_company_suffix_stripped():
    assert n.norm_company("北京智谱华章科技有限公司") == "北京智谱华章科技"
    assert n.norm_company("Acme Technologies Limited") == "acme technologies"
    assert n.norm_company("Acme Tech Ltd.") == "acme tech"

def test_title_bracket_and_spaces():
    assert n.norm_title("大模型应用开发实习生（深圳）【急】") == "大模型应用开发实习生"
    assert n.norm_title("  Data  Analyst Intern (2027) ") == "data analyst intern"

def test_location_city():
    assert n.norm_location("深圳·南山区") == "深圳"
    assert n.norm_location("Hong Kong, Kowloon") == "hong kong"
    assert n.norm_location("") == ""

def test_fingerprint_stable_across_platforms():
    a = n.fingerprint("北京智谱华章科技有限公司", "大模型应用开发实习生（深圳）", "深圳·南山区")
    b = n.fingerprint("智谱华章科技有限公司", "大模型应用开发实习生", "深圳")
    assert a != b  # 公司名不同前缀不视为同一家，保守
    c = n.fingerprint("北京智谱华章科技", "大模型应用开发实习生", "深圳")
    assert a == c

def test_job_id_prefers_platform_id():
    assert n.make_job_id("boss", "abc", "x", "y", "z") == n.make_job_id("boss", "abc", "q", "w", "e")
    assert n.make_job_id("referral", "", "x", "y", "z") == n.make_job_id("referral", None, "x", "y", "z")

def test_detect_lang():
    assert n.detect_lang("我们正在招聘实习生") == "zh"
    assert n.detect_lang("We are hiring an intern") == "en"
    assert n.detect_lang("我們正在招聘實習生，負責數據分析") == "zh-hant"

def test_company_not_stripped_to_empty():
    assert n.norm_company("Group") == "group"
    assert n.norm_company("有限公司") == "有限公司"
    assert n.norm_company("Acme Group") == "acme"
