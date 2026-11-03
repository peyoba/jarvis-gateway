"""
Token usage tracking service for Jarvis Gateway.
Probes OpenToken if available; gracefully falls back to local session token estimator.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import date
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from jarvis_gateway.llm.client import LLMUsage

# UI progress reference only, not an account balance. Sized for the "with cache"
# basis used by the TokenRank leaderboard default (cache reads dominate usage).
REFERENCE_DAILY_TOKENS = 50_000_000

TOKEN_BASIS = "with_cache"  # input + output + cache_read + cache_write


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
        self._local_estimated = False
        self._local_incomplete = False
        self._llm_calls = 0
        self._external: Optional[Dict[str, int]] = None
        self._external_normalized: Optional[Dict[str, int]] = None
        self._external_cache: Dict[str, int] = {}
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
            self._local_estimated = False
            self._local_incomplete = False
            self._llm_calls = 0
            self._external = None
            self._external_normalized = None
            self._external_cache = {}
            self._external_ok = False
            self._external_update = 0.0

    def add_local_tokens(self, count: int, tool_name: str = "Jarvis"):
        """始终保留本地估算；外部恢复后仅展示外部值，不相加。"""
        count = max(0, int(count))
        if not count:
            return
        with self._lock:
            self._reset_day()
            self._local_tools[tool_name] = self._local_tools.get(tool_name, 0) + count
            self._local_estimated = True
            self._local_update = time.time()

    def add_llm_usage(self, usage: LLMUsage):
        """优先记录返回的供应商用量；零调用不增 Token，失败账单显式标为未知。"""
        if not usage.calls:
            return
        with self._lock:
            self._reset_day()
            self._llm_calls += usage.calls
            if usage.total_tokens:
                self._local_tools["Jarvis"] = self._local_tools.get("Jarvis", 0) + usage.total_tokens
            self._local_estimated |= usage.estimated
            self._local_incomplete |= usage.incomplete
            self._local_update = time.time()

    def _fetch_opentoken_usage(self) -> Optional[Dict[str, Any]]:
        """Read today's usage from `opentoken preview --json` in both bases.

        "with_cache" (input + output + cache_read + cache_write) matches the
        TokenRank leaderboard default; "normalized" (input + output) is what the
        CLI's text table prints. Returns None when the bridge is unavailable.
        """
        if not self.opentoken_bin:
            return None
        today_str = date.today().isoformat()
        try:
            p = subprocess.run([self.opentoken_bin, "preview", "--json"],
                               capture_output=True, text=True, timeout=15)
            if p.returncode != 0:
                return None
            payload = json.loads(p.stdout)
        except Exception:
            return None
        rows = payload.get("rows") or []
        if not rows:
            return None
        tools: Dict[str, int] = {}
        normalized: Dict[str, int] = {}
        totals = {k: 0 for k in ("with_cache", "normalized", "input", "output",
                                 "cache_read", "cache_write")}
        for row in rows:
            if not isinstance(row, dict) or row.get("date") != today_str:
                continue
            name = row.get("tool") or "unknown"
            try:
                inp = int(row.get("input", 0) or 0)
                out = int(row.get("output", 0) or 0)
                cache_read = int(row.get("cache_read", 0) or 0)
                cache_write = int(row.get("cache_write", 0) or 0)
                norm = int(row.get("normalized", inp + out) or 0)
            except (TypeError, ValueError):
                continue
            if min(inp, out, cache_read, cache_write, norm) < 0:
                continue
            with_cache = inp + out + cache_read + cache_write
            tools[name] = tools.get(name, 0) + with_cache
            normalized[name] = normalized.get(name, 0) + norm
            totals["with_cache"] += with_cache
            totals["normalized"] += norm
            totals["input"] += inp
            totals["output"] += out
            totals["cache_read"] += cache_read
            totals["cache_write"] += cache_write
        if not tools:
            return None
        tools["TOTAL"] = totals["with_cache"]
        normalized["TOTAL"] = totals["normalized"]
        return {"tools": tools, "normalized": normalized, "totals": totals}

    def _fetch_opentoken_stats_text(self) -> Optional[Dict[str, int]]:
        """Legacy fallback: the text table only reports the normalized metric."""
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

    def _fetch_opentoken_stats(self) -> Optional[Dict[str, int]]:
        """With-cache tool totals, falling back to the normalized text table."""
        usage = self._fetch_opentoken_usage()
        if usage:
            return dict(usage["tools"])
        return self._fetch_opentoken_stats_text()

    def _poll_once(self):
        day = date.today()
        usage = self._fetch_opentoken_usage()
        if usage:
            raw_stats: Optional[Dict[str, int]] = dict(usage["tools"])
            normalized = dict(usage["normalized"])
            cache_totals = dict(usage["totals"])
        else:
            raw_stats = self._fetch_opentoken_stats_text()
            normalized = dict(raw_stats) if raw_stats else {}
            cache_totals = {}
        with self._lock:
            self._reset_day()
            self._external_ok = raw_stats is not None and day == self._day
            if self._external_ok:
                self._external = dict(raw_stats)
                self._external_normalized = normalized
                self._external_cache = cache_totals
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
            normalized_tools = dict(self._external_normalized or tools) if external else dict(tools)
            cache_totals = dict(self._external_cache or {}) if external else {}
            total_normalized = (
                normalized_tools.get("TOTAL", total) if external else total
            )
            return copy.deepcopy({
                "total": total, "total_str": format_tokens(total),
                "total_normalized": total_normalized,
                "total_normalized_str": format_tokens(total_normalized),
                "basis": TOKEN_BASIS if external else "session",
                "input": int(cache_totals.get("input", 0)),
                "output": int(cache_totals.get("output", 0)),
                "cache_read": int(cache_totals.get("cache_read", 0)),
                "cache_write": int(cache_totals.get("cache_write", 0)),
                "tools_normalized": normalized_tools,
                "pct": min(100, int(total / REFERENCE_DAILY_TOKENS * 100)),
                "line1": f"{active[0][0]}: {format_tokens(active[0][1])}" if active else "全部空闲",
                "line2": " | ".join(f"{k}: {format_tokens(v)}" for k, v in active[1:3]) or "全部空闲",
                "labels": labels, "tools": tools,
                "active_tool": active[0][0] if active else "Jarvis",
                "last_update": self._external_update if external else self._local_update,
                "is_busy": bool(active),
                "source": "OpenToken" if external else (
                    "GatewayUsage" if self._llm_calls and not self._local_estimated else "LocalEstimator"
                ),
                "health": "ok" if external else ("degraded" if self.opentoken_bin else "local"),
                "external_last_update": self._external_update,
                "estimated": not external and self._local_estimated,
                "incomplete": not external and self._local_incomplete,
                "llm_calls": self._llm_calls,
                "date": self._day.isoformat(),
            })

    def get_stats_text(self) -> str:
        data = self.get_snapshot()
        total = data.get("total", 0)
        if data.get("source") != "OpenToken":
            label = "估算使用" if data.get("estimated") else "已知用量"
            unknown = "失败或超时请求的额外用量未知。" if data.get("incomplete") else ""
            return f"本次网关会话{label} {data.get('total_str', '0')} Token，非全账户账单。" + unknown
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
            "today_tokens_normalized_str": stats.get("total_normalized_str", "--"),
            "basis": stats.get("basis", TOKEN_BASIS),
            "cache_read": stats.get("cache_read", 0),
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
