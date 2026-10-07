from __future__ import annotations

import json

import pytest

from jp import backends, resume


def test_changed_text_cannot_confirm_stale_draft(tmp_path):
    first = resume.make_draft(tmp_path, "熟悉 Python", {"llm": {"backend": "manual"}})
    resume.make_draft(tmp_path, "熟悉 SQL", {"llm": {"backend": "manual"}})
    with pytest.raises(resume.ResumeError, match="ID"):
        resume.confirm_draft(tmp_path, first["id"], [])


def test_same_resume_input_reuses_validated_output(tmp_path, monkeypatch):
    text = "熟悉 Python"
    resume.make_draft(tmp_path, text, {"llm": {"backend": "manual"}}, True)
    output = {"items": [{"id": "resume.python", "type": "skill", "title": "Python", "text": text,
                         "source_quote": text, "confirmed": False, "inferred": False}]}
    (tmp_path / "tasks" / "_resume" / "resume" / "output.json").write_text(json.dumps(output), encoding="utf-8")
    monkeypatch.setattr(backends, "run_api", lambda *_: pytest.fail("不得重复调用模型"))
    cached = resume.make_draft(tmp_path, text, {"llm": {"backend": "api"}}, True)
    assert cached["status"] == "ready_for_review" and cached["items"] == output["items"]
