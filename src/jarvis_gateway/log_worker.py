"""启动器专用的标准库日志代理；持续滚动日志，转发退出信号。"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 2


def make_log_handler(path: Path) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    for existing in (path, *(Path(f"{path}.{i}") for i in range(1, LOG_BACKUPS + 1))):
        if existing.exists():
            existing.chmod(0o600)
    # umask 同时保护首次创建和后续滚动的新文件。
    os.umask(0o077)
    handler = RotatingFileHandler(path, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    path.chmod(0o600)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    return handler


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    path = Path(args.pop(0))
    redact = args[0] == "--redact"
    args.pop(0)  # --redact 或 --plain
    logger = logging.getLogger("jarvis-process")
    logger.setLevel(logging.INFO)
    handler = make_log_handler(path)
    logger.addHandler(handler)
    proc = None
    stopping = False

    def stop_child(signum, frame):
        nonlocal stopping
        stopping = True
        if proc is not None and proc.poll() is None:
            proc.terminate()

    try:
        # 先注册处理器；信号若发生在 Popen 期间，创建完成后再转发。
        signal.signal(signal.SIGTERM, stop_child)
        signal.signal(signal.SIGINT, stop_child)
        proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        if stopping:
            proc.terminate()
        while True:
            chunk = proc.stdout.read1(16384)
            if not chunk:
                break
            if redact:
                # whisper-server 自己可能打印识别正文，不直接落盘。
                logger.info("[ASR] 已抑制 %d 字节子进程输出", len(chunk))
            else:
                logger.info("%s", chunk.decode("utf-8", "replace").rstrip())
        code = proc.wait()
        logger.info("子进程退出，exit=%d", code)
        return code if code >= 0 else 128 - code
    except Exception as exc:
        logger.error("日志代理失败，type=%s", type(exc).__name__)
        return 1
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            proc.stdout.close()
        handler.close()
        logger.removeHandler(handler)


if __name__ == "__main__":
    sys.exit(main())
