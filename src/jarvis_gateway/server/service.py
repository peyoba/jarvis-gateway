"""
控制台里的服务控制：启动 / 停止 / 重启、登录自启、日志尾巴。

菜单栏 App 里能点的按钮，网页控制台上都有；真正的进程操作仍然交给
`jarvis` 启动器执行，避免控制台和命令行各维护一套停止逻辑。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

LABEL = "ai.jarvis.gateway"
ACTIONS = ("start", "stop", "restart")
# 网页里的一次「开启/关闭登录自启」对应启动器的 autostart 子命令。
AUTOSTART_ACTIONS = {"autostart_on": "on", "autostart_off": "off"}

LOG_FILES = {
    "gateway": "jarvis_gateway.log",
    "whisper": "whisper_server.log",
    "ip_guard": "ip_guard.log",
    "autostart": "autostart.log",
    "control": "control.log",
}
LOG_TAIL_BYTES = 96 * 1024
AUTOSTART_CACHE_SEC = 3.0
WAIT_TIMEOUT_SEC = 45.0

Spawn = Callable[[list, Path], Tuple[bool, str, Any]]


def _timestamp() -> str:
    return time.strftime("%H:%M:%S")


def _default_launchctl(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=5)


def _detached_spawn(cmd: list, log_path: Path, cwd: Optional[str] = None) -> Tuple[bool, str, Any]:
    """先等一秒再执行，让 HTTP 响应先写回浏览器；再用新会话脱离本进程组。

    停止 / 重启会把当前提供网页的进程一起结束，所以这条辅助命令必须既
    比响应晚一步，又不能跟着网关的进程组一起被 kill 掉（停止用 killpg）。
    """
    line = " ".join(shlex.quote(str(part)) for part in cmd)
    script = "sleep 1; exec %s" % line
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as log:
            log.write(("\n[%s] %s\n" % (_timestamp(), line)).encode("utf-8"))
            log.flush()
            proc = subprocess.Popen(
                ["/bin/bash", "-c", script],
                cwd=cwd or None,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
    except OSError as exc:
        return False, str(exc), None
    return True, "", proc


class ServiceControl:
    """把「菜单栏里那几个动作」变成控制台可调用的接口。"""

    def __init__(
        self,
        root_dir: str = "",
        *,
        spawn: Optional[Spawn] = None,
        launchctl: Optional[Callable[[list], Any]] = None,
        launcher: Optional[Path] = None,
        python: Optional[Path] = None,
        plist: Optional[Path] = None,
    ):
        root = Path(root_dir) if root_dir else Path(__file__).resolve().parents[3]
        self.root = root
        self.log_dir = root / "logs"
        self.launcher = Path(launcher) if launcher else root / "jarvis"
        self.python = Path(python) if python else root / "venv" / "bin" / "python"
        self.plist_path = Path(plist) if plist else (
            Path.home() / "Library" / "LaunchAgents" / ("%s.plist" % LABEL)
        )
        self._spawn: Spawn = spawn or (lambda cmd, path: _detached_spawn(cmd, path, cwd=str(root)))
        self._launchctl = launchctl or _default_launchctl

        self._lock = threading.Lock()
        self._busy = False
        self._last: Dict[str, Any] = {}
        self._cache: Dict[str, Any] = {"ts": 0.0, "data": None}

    # ------------------------------------------------------------------ #
    # 启动 / 停止 / 重启
    # ------------------------------------------------------------------ #
    def action(self, name: str) -> Dict[str, Any]:
        if name in ACTIONS:
            argv = [str(self.python), str(self.launcher), name]
        elif name in AUTOSTART_ACTIONS:
            argv = [str(self.python), str(self.launcher), "autostart", AUTOSTART_ACTIONS[name]]
            self._cache["data"] = None
        else:
            return {"error": "unknown_action", "detail": "不认识的动作：%s" % name}

        if not self._lock.acquire(blocking=False):
            return {"error": "busy", "detail": "上一次操作还没结束，稍等一下"}

        try:
            ok, detail, proc = self._spawn(argv, self.log_dir / "control.log")
        except Exception as exc:  # noqa: BLE001 - 任何启动失败都要把锁还回去
            ok, detail, proc = False, str(exc), None
        if not ok or proc is None:
            self._busy = False
            self._lock.release()
            self._last = {"action": name, "ok": False, "detail": detail, "at": _timestamp()}
            return {"error": "spawn_failed", "detail": detail}

        self._busy = True
        self._last = {"action": name, "ok": True, "detail": "", "at": _timestamp()}
        threading.Thread(target=self._wait, args=(proc,), daemon=True).start()
        return {"started": True, "action": name, "at": self._last["at"]}

    def _wait(self, proc) -> None:
        try:
            proc.wait(timeout=WAIT_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            pass
        except Exception:  # noqa: BLE001 - 等不到也要解锁
            pass
        finally:
            self._busy = False
            try:
                self._lock.release()
            except RuntimeError:
                pass

    # ------------------------------------------------------------------ #
    # 登录自启
    # ------------------------------------------------------------------ #
    def _job_loaded(self) -> bool:
        try:
            if self._launchctl(["launchctl", "print", "gui/%d/%s" % (os.getuid(), LABEL)]).returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            return False
        try:
            listing = self._launchctl(["launchctl", "list"])
        except (OSError, subprocess.SubprocessError):
            return False
        return LABEL in (getattr(listing, "stdout", "") or "")

    def autostart(self) -> Dict[str, Any]:
        now = time.time()
        if self._cache["data"] is not None and now - self._cache["ts"] < AUTOSTART_CACHE_SEC:
            return self._cache["data"]
        data = {
            "label": LABEL,
            "enabled": self.plist_path.exists(),
            "loaded": self._job_loaded(),
            "plist": str(self.plist_path),
        }
        self._cache.update(ts=now, data=data)
        return data

    # ------------------------------------------------------------------ #
    # 日志尾巴
    # ------------------------------------------------------------------ #
    def log_names(self) -> list:
        return list(LOG_FILES)

    def log_tail(self, name: str, lines: int = 60) -> Dict[str, Any]:
        if name not in LOG_FILES:
            return {"error": "unknown_log", "detail": "可用日志：%s" % "、".join(LOG_FILES)}
        lines = max(1, min(int(lines or 60), 200))
        path = self.log_dir / LOG_FILES[name]
        if not path.exists():
            return {"name": name, "file": str(path), "lines": [], "empty": True}
        try:
            size = path.stat().st_size
            with open(path, "rb") as fh:
                if size > LOG_TAIL_BYTES:
                    fh.seek(size - LOG_TAIL_BYTES)
                chunk = fh.read()
        except OSError as exc:
            return {"error": "read_failed", "detail": str(exc)}
        text = chunk.decode("utf-8", "replace").splitlines()[-lines:]
        return {"name": name, "file": str(path), "lines": text}

    # ------------------------------------------------------------------ #
    # 快照
    # ------------------------------------------------------------------ #
    def snapshot(self, pid: int = 0, port: int = 0, uptime: int = 0) -> Dict[str, Any]:
        return {
            "pid": pid or os.getpid(),
            "port": port,
            "uptime": uptime,
            "busy": self._busy,
            "last": self._last,
            "autostart": self.autostart(),
            "logs": self.log_names(),
        }
