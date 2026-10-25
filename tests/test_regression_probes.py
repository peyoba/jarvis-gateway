"""将审查探针纳入常规回归；探针必须断言结果，而非仅输出 JSON。"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBES = ROOT / "docs/reviews/2026-10-23"


def test_python_review_regressions(tmp_path):
    env = {**os.environ, "REVIEW_REPO": str(ROOT), "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [sys.executable, str(PROBES / "probe_python.py")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)) >= 19


def test_browser_review_regressions(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("网页状态机回归需要 Node.js；CI 会显式安装")
    result = subprocess.run(
        [node, str(PROBES / "probe_dashboard.js")], cwd=tmp_path,
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    results = json.loads(result.stdout)
    assert len(results) >= 8
    assert all(row["passed"] for row in results)
