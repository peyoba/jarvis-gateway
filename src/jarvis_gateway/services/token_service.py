"""
Token usage tracking service for Jarvis Gateway.
Probes OpenToken if available; gracefully falls back to local session token estimator.
"""

from __future__ import annotations

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

        # In-memory metrics state
        self._data: Dict[str, Any] = {
            "total": 0,
            "total_str": "--",
            "pct": 0,
            "line1": "Jarvis",
            "line2": "正在连接...",
            "labels": ["总计  --", "Jarvis  --", "Claude  --", "Codex  --"],
            "tools": {},
            "active_tool": "Jarvis",
            "last_update": 0,
            "is_busy": False,
            "source": "OpenToken" if self.opentoken_bin else "LocalEstimator",
        }

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="TokenService")
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def add_local_tokens(self, count: int, tool_name: str = "Jarvis"):
        """Record tokens from local Gateway LLM interactions."""
        with self._lock:
            # If not using external OpenToken or OpenToken gave 0
            if not self.opentoken_bin:
                new_total = self._data["total"] + count
                pct = min(100, int((new_total / REFERENCE_DAILY_TOKENS) * 100))
                self._data["total"] = new_total
                self._data["total_str"] = format_tokens(new_total)
                self._data["pct"] = pct
                self._data["line1"] = f"{tool_name}: {format_tokens(new_total)}"
                self._data["line2"] = "全部空闲"
                self._data["labels"] = [f"总计  {format_tokens(new_total)}", f"{tool_name}  {format_tokens(new_total)}", "空闲", "空闲"]
                self._data["last_update"] = time.time()
                self._data["is_busy"] = True

    def _fetch_opentoken_stats(self) -> Optional[Dict[str, int]]:
        if not self.opentoken_bin:
            return None
        today_str = date.today().isoformat()
        cmd = [self.opentoken_bin, "preview"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
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
                    res = {}
                    for t, val_s in zip(tools, parts):
                        try:
                            res[t] = int(val_s.replace(",", ""))
                        except ValueError:
                            res[t] = 0
                    return res
        except Exception:
            pass
        return None

    def _worker(self):
        tool_name_map = {
            "antigravity": "Antigravity",
            "claude-code": "Claude Code",
            "hermes": "Hermes",
            "codex": "Codex",
            "cursor": "Cursor",
            "gemini": "Gemini",
            "grok": "Grok",
        }

        while not self._stop_event.is_set():
            if self.opentoken_bin:
                raw_stats = self._fetch_opentoken_stats()
                if raw_stats:
                    total = raw_stats.get("TOTAL", 0)
                    active_tools = []
                    for k, v in raw_stats.items():
                        if k != "TOTAL" and v > 0:
                            name = tool_name_map.get(k, k.capitalize())
                            active_tools.append((name, k, v))
                    active_tools.sort(key=lambda x: x[2], reverse=True)

                    labels = [f"总计  {format_tokens(total)}"]
                    for name, _, val in active_tools[:3]:
                        labels.append(f"{name}  {format_tokens(val)}")
                    while len(labels) < 4:
                        labels.append("空闲")

                    line1 = f"{active_tools[0][0]}: {format_tokens(active_tools[0][2])}" if active_tools else "全部空闲"
                    line2_parts = [
                        f"{('Claude' if name == 'Claude Code' else name)}: {format_tokens(val)}"
                        for name, _, val in active_tools[1:3]
                    ]
                    line2 = " | ".join(line2_parts) if line2_parts else "全部空闲"
                    pct = min(100, int((total / REFERENCE_DAILY_TOKENS) * 100))

                    with self._lock:
                        self._data.update({
                            "total": total,
                            "total_str": format_tokens(total),
                            "pct": pct,
                            "line1": line1,
                            "line2": line2,
                            "labels": labels,
                            "tools": raw_stats,
                            "active_tool": active_tools[0][1] if active_tools else "AI",
                            "last_update": time.time(),
                            "is_busy": True,
                        })

            self._stop_event.wait(self.poll_interval)

    def get_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def get_stats_text(self) -> str:
        data = self.get_snapshot()
        total = data.get("total", 0)
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
