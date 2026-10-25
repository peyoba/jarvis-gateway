"""
Token usage tracking service for Jarvis Gateway.
Probes OpenToken if available; gracefully falls back to local session token estimator.
"""

from __future__ import annotations

import copy
import os
import shutil
import subprocess
import threading
import time
from datetime import date
from typing import Any, Dict, Optional

REFERENCE_DAILY_TOKENS = 5_000_000  # UI progress reference only, not an account balance


def format_tokens(n: int) -> str:
    """Format token count into compact readable string (万 / 千)."""
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}千"
    else:
        return str(n)


class TokenService:
    def __init__(self, opentoken_bin: str | None = None, poll_interval: float = 5.0):
        # Resolve opentoken binary
        resolved = None
        if opentoken_bin:
            expanded = os.path.expanduser(opentoken_bin)
            if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
                resolved = expanded
        if not resolved:
            resolved = shutil.which("opentoken")

        self.opentoken_bin = resolved
        self.poll_interval = poll_interval
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._day = date.today()
        self._local_tools: Dict[str, int] = {}
        self._local_update = 0.0
        self._external: Optional[Dict[str, int]] = None
        self._external_update = 0.0
        self._external_ok = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="TokenService")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.ident != threading.get_ident():
            self._thread.join(timeout=11)

    def _reset_day(self):
        today = date.today()
        if today != self._day:
            self._day = today
            self._local_tools = {}
            self._local_update = 0.0
            self._external = None
            self._external_ok = False
            self._external_update = 0.0

    def add_local_tokens(self, count: int, tool_name: str = "Jarvis"):
        """始终保留本地估算；外部恢复后仅展示外部值，不相加。"""
        with self._lock:
            self._reset_day()
            self._local_tools[tool_name] = self._local_tools.get(tool_name, 0) + max(0, int(count))
            self._local_update = time.time()

    def _fetch_opentoken_stats(self) -> Optional[Dict[str, int]]:
        if not self.opentoken_bin:
            return None
        today_str = date.today().isoformat()
        cmd = [self.opentoken_bin, "preview"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if p.returncode != 0:
                return None
            lines = p.stdout.splitlines()
            header_idx = -1
            for i, line in enumerate(lines):
                if "Daily leaderboard metric" in line:
                    header_idx = i + 2
                    break
            if header_idx == -1 or header_idx >= len(lines):
                return None
            tools = lines[header_idx].split()[1:]
            for i in range(header_idx + 2, len(lines)):
                line = lines[i]
                if line.startswith("Date"):
                    break
                if line.startswith(today_str):
                    parts = line.split()[1:]
                    if len(parts) != len(tools) or len(set(tools)) != len(tools):
                        return None
                    res = {}
                    for t, val_s in zip(tools, parts):
                        try:
                            res[t] = int(val_s.replace(",", ""))
                        except ValueError:
                            return None
                    if "TOTAL" in res and all(v >= 0 for v in res.values()):
                        return res
                    return None
        except Exception:
            pass
        return None

    def _poll_once(self):
        day = date.today()
        raw_stats = self._fetch_opentoken_stats()
        with self._lock:
            self._reset_day()
            self._external_ok = raw_stats is not None and day == self._day
            if self._external_ok:
                self._external = dict(raw_stats)
                self._external_update = time.time()

    def _worker(self):
        while not self._stop_event.is_set():
            if self.opentoken_bin:
                self._poll_once()
            self._stop_event.wait(self.poll_interval)

    def get_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self._reset_day()
            external = (
                self._external_ok and self._external is not None
                and time.time() - self._external_update <= max(30, self.poll_interval * 3)
            )
            tools = dict(self._external if external else self._local_tools)
            total = tools.get("TOTAL", 0) if external else sum(tools.values())
            active = sorted(((k, v) for k, v in tools.items() if k != "TOTAL" and v > 0),
                            key=lambda item: item[1], reverse=True)
            labels = [f"总计  {format_tokens(total)}"]
            labels.extend(f"{k}  {format_tokens(v)}" for k, v in active[:3])
            labels.extend(["空闲"] * (4 - len(labels)))
            return copy.deepcopy({
                "total": total, "total_str": format_tokens(total),
                "pct": min(100, int(total / REFERENCE_DAILY_TOKENS * 100)),
                "line1": f"{active[0][0]}: {format_tokens(active[0][1])}" if active else "全部空闲",
                "line2": " | ".join(f"{k}: {format_tokens(v)}" for k, v in active[1:3]) or "全部空闲",
                "labels": labels, "tools": tools,
                "active_tool": active[0][0] if active else "Jarvis",
                "last_update": self._external_update if external else self._local_update,
                "is_busy": bool(active), "source": "OpenToken" if external else "LocalEstimator",
                "health": "ok" if external else ("degraded" if self.opentoken_bin else "local"),
                "external_last_update": self._external_update, "estimated": not external,
                "date": self._day.isoformat(),
            })

    def get_stats_text(self) -> str:
        data = self.get_snapshot()
        total = data.get("total", 0)
        if data.get("estimated"):
            return f"本次网关会话估算使用 {data.get('total_str', '0')} Token，非全账户账单。"
        if not total:
            return "今天还没有消耗 Token。"
        total_str = data.get("total_str") or f"{total/10000:.1f}万"
        line1 = (data.get("line1") or "").strip()
        extra = f"，其中 {line1}" if line1 and line1 != "全部空闲" else ""
        return f"今天累计消耗了 {total_str} Token{extra}。"

    def get_cursor_status_payload(self) -> Dict[str, Any]:
        """Payload for ESP32 /cursor-status polling."""
        stats = self.get_snapshot()
        return {
            # Legacy firmware fields. This gateway has no Cursor hook, so report
            # neutral values instead of pretending a Cursor agent is running.
            "cursor_running": False,
            "mode": "gateway",
            "agent_active": stats.get("is_busy", False),
            "hook_status": "local",
            "hook_event": "token_update",
            "hook_message": f"今日: {stats.get('total_str', '--')}",
            "source": stats.get("source", "OpenToken"),
            "ai_model": stats.get("active_tool", "Jarvis"),
            "today_tokens": stats.get("total", 0),
            "today_tokens_str": stats.get("total_str", "--"),
            "pct": stats.get("pct", 0),
            "title": "今日用量",
            "account_balance": "--",
            "deepseek_balance": "--",
            "line1": stats.get("line1", "--"),
            "line2": stats.get("line2", "--"),
            "labels": stats.get("labels", []),
        }

    def get_deepseek_balance_payload(self) -> Dict[str, Any]:
        """Legacy ESP32 shape, but never invent an account balance.

        OpenAI-compatible providers do not share a balance API. The gateway only
        knows local token usage, so it reports the balance as unavailable instead
        of returning a fabricated number.
        """
        stats = self.get_snapshot()
        return {
            "is_available": False,
            "balance_str": "--",
            "balance_infos": [],
            "today_tokens": stats.get("total", 0),
            "today_tokens_str": stats.get("total_str", "--"),
            "pct": stats.get("pct", 0),
            "note": "Provider balance is not exposed by Jarvis Gateway.",
        }
