"""开发板地址联动。

把网关所在 Mac 的局域网地址通过 USB 串口写进开发板（`>hatch.host=<ip>`）。
Web 控制台的「开发板地址」面板直接调用这里，不必再开一个独立 App。

串口打开会让板子复位、约 7 秒后重新联网，所以写入放在后台线程里，
接口立刻返回，控制台轮询 :meth:`BoardLink.snapshot` 拿结果。
"""

from __future__ import annotations

import ipaddress
import threading
import time
from typing import Callable, Dict, Optional

from jarvis_gateway import ip_sync


def _clock() -> str:
    return time.strftime("%H:%M:%S")


class BoardLink:
    """串口写入开发板地址的一次性任务 + 最近结果。"""

    def __init__(
        self,
        run_dir=None,
        push: Optional[Callable[..., tuple]] = None,
        lan_ip: Optional[Callable[[], str]] = None,
        find_port: Optional[Callable[[], Optional[str]]] = None,
    ):
        self.run_dir = run_dir
        self._push = push or ip_sync.push_host
        self._lan_ip = lan_ip or ip_sync.lan_ip
        self._find_port = find_port or ip_sync.find_serial_port
        self._thread: Optional[threading.Thread] = None
        self._last: Dict[str, object] = {}

    # ------------------------------------------------------------------ #
    # 只读状态
    # ------------------------------------------------------------------ #
    def snapshot(self) -> Dict[str, object]:
        busy = bool(self._thread and self._thread.is_alive())
        try:
            applied = ip_sync.read_applied(self.run_dir)
        except OSError:
            applied = ""
        try:
            port = self._find_port() or ""
        except Exception:  # 串口枚举失败不该拖垮整个控制台
            port = ""
        try:
            lan = self._lan_ip()
        except Exception:
            lan = ""
        return {
            "lan_ip": lan,
            "board_host": applied,
            "serial_port": port,
            "busy": busy,
            "last": dict(self._last),
        }

    # ------------------------------------------------------------------ #
    # 触发写入
    # ------------------------------------------------------------------ #
    def sync(self, host: str = "") -> Dict[str, object]:
        """host 为空＝把当前 Mac 地址推给板子；非空＝手动指定地址。"""
        if self._thread and self._thread.is_alive():
            return {"started": False, "error": "busy", "detail": "上一次同步还没结束"}

        manual = bool(host.strip())
        target = host.strip() if manual else self._lan_ip()
        try:
            ipaddress.IPv4Address(target)
        except ValueError:
            return {
                "started": False,
                "error": "invalid_host",
                "detail": "不是合法的 IPv4 地址：%s" % (target or "(空)"),
            }

        if not manual and ip_sync.read_applied(self.run_dir) == target:
            self._last = {
                "ok": True,
                "host": target,
                "port": self._find_port() or "",
                "detail": "板子已经是这个地址，跳过串口写入",
                "at": _clock(),
            }
            return {"started": False, "skipped": True, "host": target}

        self._thread = threading.Thread(
            target=self._run, args=(target,), name="board-sync", daemon=True
        )
        self._thread.start()
        return {"started": True, "host": target}

    def wait(self, timeout: float = 20.0) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # ------------------------------------------------------------------ #
    # 工作线程
    # ------------------------------------------------------------------ #
    def _run(self, target: str) -> None:
        started = time.time()
        try:
            ok, detail, port = self._push(target)
        except Exception as exc:  # 串口层任何异常都只记结果，不让线程裸崩
            ok, detail, port = False, "串口写入异常：%s" % exc, None
        if ok:
            try:
                ip_sync.write_applied(target, self.run_dir)
            except OSError as exc:
                ok, detail = False, "地址写进板子了，但状态文件保存失败：%s" % exc
        self._last = {
            "ok": bool(ok),
            "host": target,
            "port": port or "",
            "detail": str(detail),
            "at": _clock(),
            "elapsed": round(time.time() - started, 1),
        }
        print(
            "[board] %s %s -> %s" % ("ok" if ok else "失败", target, self._last["detail"]),
            flush=True,
        )
