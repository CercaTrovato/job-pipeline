from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys


def test_chinese_diagnosis_in_cp1252_redirected_terminal(tmp_path):
    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    result = subprocess.run(
        [sys.executable, "-m", "jp.cli", "--workspace", str(tmp_path / "workspace"), "doctor", "--json"],
        cwd=pathlib.Path(__file__).resolve().parents[1], env=env,
        capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    diagnosis = json.loads(result.stdout.decode("utf-8"))
    assert diagnosis["status"] == "manual" and "人工" in diagnosis["message"]
