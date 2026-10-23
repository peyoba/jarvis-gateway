"""
Thread-safe runtime state management and status snapshot builder.
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from typing import Any, Dict, List


class GatewayState:
    def __init__(self, default_mode: str = "auto", speak_typed: bool = False, root_dir: str = ""):
        self.start_time = time.time()
        self.root_dir = root_dir
        self._mode = default_mode
        self._speak_typed = speak_typed

        self._lock = threading.Lock()
        self._recent_turns: List[Dict[str, Any]] = []
        self._stats = {"turns": 0, "errors": 0}

        self._status_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
        self._lan_ip_cache: Dict[str, Any] = {"ip": None, "ts": 0.0}

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def set_mode(self, new_mode: str) -> bool:
        new_mode = new_mode.lower()
        if new_mode in ("auto", "raw", "polish", "dialog"):
            with self._lock:
                self._mode = new_mode
                self._status_cache["ts"] = 0.0
            print(f"[*] 全局输入模式已切换至: {new_mode}", flush=True)
            return True
        return False

    def toggle_mode(self) -> str:
        modes = ["auto", "raw", "polish", "dialog"]
        with self._lock:
            idx = (modes.index(self._mode) + 1) % len(modes) if self._mode in modes else 0
            self._mode = modes[idx]
            self._status_cache["ts"] = 0.0
            current = self._mode
        print(f"[*] 模式轮换切换为: {current}", flush=True)
        return current

    @property
    def speak_typed(self) -> bool:
        with self._lock:
            return self._speak_typed

    def set_speak_typed(self, enabled: bool):
        with self._lock:
            self._speak_typed = enabled
            self._status_cache["ts"] = 0.0

    def record_turn(
        self,
        heard: str,
        reply: str,
        typed: str,
        mode: str,
        elapsed: float,
        kind: str = "ok",
    ):
        with self._lock:
            self._stats["turns"] += 1
            if kind == "error":
                self._stats["errors"] += 1
            self._recent_turns.insert(0, {
                "ts": time.time(),
                "mode": mode or "-",
                "heard": heard or "",
                "reply": reply or "",
                "typed": typed or "",
                "elapsed": round(elapsed, 2),
                "kind": kind,
            })
            del self._recent_turns[12:]
            self._status_cache["ts"] = 0.0

    def get_lan_ip(self) -> str:
        now = time.time()
        if self._lan_ip_cache["ip"] and (now - self._lan_ip_cache["ts"] < 60):
            return self._lan_ip_cache["ip"]

        for iface in ("en0", "en1"):
            try:
                out = subprocess.run(
                    ["ipconfig", "getifaddr", iface],
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                )
                ip = out.stdout.strip()
                if ip and not ip.startswith("127."):
                    self._lan_ip_cache.update(ip=ip, ts=now)
                    return ip
            except Exception:
                pass

        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        except Exception:
            ip = "127.0.0.1"
        finally:
            if s is not None:
                s.close()

        self._lan_ip_cache.update(ip=ip, ts=now)
        return ip

    def build_status(
        self,
        port: int,
        whisper_port: int,
        whisper_online: bool,
        data_port: int,
        legacy_enabled: bool,
        token_snapshot: Dict[str, Any],
        weather_snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            if self._status_cache["data"] is not None and (now - self._status_cache["ts"] < 2.0):
                return self._status_cache["data"]

            recent = [dict(x) for x in self._recent_turns]
            stats = dict(self._stats)
            mode = self._mode
            speak_typed = self._speak_typed

        desc = (weather_snapshot.get("description") or "").strip()
        data = {
            "gateway": {
                "online": True,
                "pid": os.getpid(),
                "root": self.root_dir,
                "port": port,
                "uptime": int(now - self.start_time),
                "mode": mode,
                "speak_typed": speak_typed,
            },
            "whisper": {
                "online": whisper_online,
                "port": whisper_port,
            },
            "data": {
                "online": True,
                "port": data_port if legacy_enabled else port,
                "legacy_enabled": legacy_enabled,
                "tokens_str": token_snapshot.get("total_str") or "--",
                "tokens_total": token_snapshot.get("total", 0),
                "tokens_line": token_snapshot.get("line1") or "",
                "weather": f"{weather_snapshot.get('location', '')} {weather_snapshot.get('temperature', '')}°C {desc}".strip(),
            },
            "stats": stats,
            "recent": recent,
            "lan_ip": self.get_lan_ip(),
        }

        with self._lock:
            self._status_cache["ts"] = now
            self._status_cache["data"] = data

        return data
