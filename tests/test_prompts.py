from __future__ import annotations
import pathlib
from jp import prompts

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPECS = ROOT / "llm" / "specs"

def test_render_contains_spec_body_and_input():
    text = prompts.render("analyze", {"jd_text": "招聘 Python 实习生", "region": "CN"}, SPECS)
    assert "原子化拆解" in text and "招聘 Python 实习生" in text
    assert text.lstrip().startswith("# JD 分析")           # frontmatter 已去掉
    assert "schema.json" in text and "output.json" in text

def test_render_unknown_step():
    import pytest
    with pytest.raises(FileNotFoundError):
        prompts.render("nope", {}, SPECS)


def test_render_match_keeps_literal_braces():
    text = prompts.render("match", {"job_id": "j", "hard_pass": True}, SPECS)
    assert "{partial, no_evidence, gap}" in text          # 正文花括号不被 format 吃掉
    assert '"hard_pass": true' in text and "evidence_grade=E" in text
