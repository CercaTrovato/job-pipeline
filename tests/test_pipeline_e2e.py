from __future__ import annotations
import json
import pathlib
import shutil
import yaml
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys; sys.path.insert(0, str(ROOT))
import pipeline  # noqa: E402
from tests.test_steps import ANALYZE_OUT, MATCH_OUT  # noqa: E402

@pytest.fixture
def cfg_path(tmp_path):
    cfg = yaml.safe_load((ROOT / "tests" / "fixtures" / "config.yaml").read_text(encoding="utf-8"))
    cfg["paths"]["db"] = str(tmp_path / "e2e.sqlite")
    cfg["paths"]["tasks_dir"] = str(tmp_path / "tasks")
    cfg["paths"]["facts_index"] = str(tmp_path / "facts_index.json")
    cfg["paths"]["resume_repo"] = str(tmp_path / "resume")
    cfg["paths"]["candidate_profile"] = str(tmp_path / "candidate_profile.json")
    cfg["llm"]["backend"] = "manual"
    shutil.copytree(ROOT / "tests" / "fixtures" / "facts", tmp_path / "resume" / "facts")
    prof = json.loads((ROOT / "tests" / "fixtures" / "jd" / "profile_good.json").read_text(encoding="utf-8"))
    (tmp_path / "candidate_profile.json").write_text(json.dumps({"facts_hash": "h", **prof}, ensure_ascii=False), encoding="utf-8")
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return p

def _run(cfg_path, *args, capsys=None):
    pipeline.main(["--config", str(cfg_path), *args])

def test_referral_to_pending_review(cfg_path, tmp_path, capsys):
    _run(cfg_path, "init-db")
    _run(cfg_path, "facts-index")
    _run(cfg_path, "ingest", "--source", "referral", "--region", "CN", "--file", str(ROOT / "tests" / "fixtures" / "jd" / "referral_cn.json"))
    _run(cfg_path, "run")
    out = capsys.readouterr().out
    assert "已生成 1 个任务包" in out
    tasks = tmp_path / "tasks"
    a_dir = next(tasks.iterdir()) / "analyze"
    assert (a_dir / "prompt.md").exists()
    (a_dir / "output.json").write_text(json.dumps(ANALYZE_OUT, ensure_ascii=False), encoding="utf-8")
    _run(cfg_path, "resume")
    out = capsys.readouterr().out
    assert "analyze 回收: 1 成功" in out and "已生成 1 个任务包" in out
    m_dir = a_dir.parent / "match"
    (m_dir / "output.json").write_text(json.dumps(MATCH_OUT, ensure_ascii=False), encoding="utf-8")
    _run(cfg_path, "resume")
    assert "match 回收: 1 成功" in capsys.readouterr().out
    _run(cfg_path, "review")
    out = capsys.readouterr().out
    assert "100.0" in out and "北京智谱华章科技有限公司" in out
    # 重跑 run / resume 不重复消耗
    _run(cfg_path, "run"); _run(cfg_path, "resume")
    assert "没有待填任务包" in capsys.readouterr().out
