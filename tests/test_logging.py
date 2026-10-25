"""日志权限、脱敏与运行中轮转；仅处理合成测试输出。"""

import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

from jarvis_gateway.log_worker import make_log_handler

ROOT = Path(__file__).resolve().parents[1]


def test_asr_worker_redacts_output_and_protects_files(tmp_path):
    path = tmp_path / "logs/whisper.log"
    result = subprocess.run([
        sys.executable, str(ROOT / "src/jarvis_gateway/log_worker.py"),
        str(path), "--redact", sys.executable,
        "-c", "print('synthetic-private-transcript')",
    ], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    text = path.read_text(encoding="utf-8")
    assert "synthetic-private-transcript" not in text
    assert "[ASR]" in text
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_logs_rotate_while_running(tmp_path):
    path = tmp_path / "logs/gateway.log"
    old_mask = os.umask(0o077)
    handler = make_log_handler(path)
    try:
        handler.maxBytes = 150
        for i in range(30):
            handler.emit(logging.LogRecord("test", logging.INFO, "", 0, f"event {i}: bounded output", (), None))
        files = list(path.parent.iterdir())
        assert len(files) == 3
        assert all(file.stat().st_size <= 150 for file in files)
        if os.name == "posix":
            assert all(stat.S_IMODE(file.stat().st_mode) == 0o600 for file in files)
    finally:
        handler.close()
        os.umask(old_mask)


def test_unknown_and_invalid_actions_do_not_persist_private_text(tmp_path):
    path = tmp_path / "logs/gateway.log"
    private = "synthetic-private-action-body"
    script = (
        "from jarvis_gateway.actions.system import SystemActionExecutor\n"
        f"private = {private!r}\n"
        "for action in (private, private + ':value', 'open_app:' + private + ';', "
        "'set_volume:' + private):\n"
        "    SystemActionExecutor.execute_action(action)\n"
    )
    result = subprocess.run([
        sys.executable, str(ROOT / "src/jarvis_gateway/log_worker.py"),
        str(path), "--plain", sys.executable, "-c", script,
    ], env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    text = path.read_text(encoding="utf-8")
    assert private not in text
    assert "unknown" in text
    assert "open_app" in text
    assert "set_volume" in text
