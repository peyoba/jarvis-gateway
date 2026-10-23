"""
Safe macOS System Automation without shell=True.
Guards against command injection while executing volume, music, and app controls.
"""

from __future__ import annotations

import datetime
import re
import subprocess
from typing import Any, Optional

SAFE_APP_PATTERN = re.compile(r"^[A-Za-z0-9 _\-]+$")

# Fast commands use *full-match* patterns on purpose. Substring matching (the old
# "天气" in text check) hijacked normal dictation and typed nothing.
_TIME_RE = re.compile(
    r"^(?:现在|当前)?(?:几点了?|几点钟|什么时间了?|什么时辰了?|"
    r"今天(?:是)?(?:几号|几月几号|星期几|周几)|明天(?:是)?(?:几号|星期几|周几))[？?。！!]*$"
)
_WEATHER_RE = re.compile(
    r"^(?:(?:查|问|看看|告诉我)(?:一下|下)?)?(?:今天|明天|现在|当前)?的?"
    r"(?:天气|气温|温度)(?:怎么样|如何|多少度|是多少)?[？?。！!]*$"
)
_TOKEN_RE = re.compile(
    r"^(?:(?:查|看|问)(?:一下|下)?)?(?:今天|当前|今日)?的?"
    r"(?:token|Token|TOKEN)(?:消耗|用量|统计|余额)?(?:多少|怎么样|情况)?[？?。！!]*$"
)
_VOLUME_RE = re.compile(
    r"^(?:请|帮我)?(?:把)?(?:音量|声音)(?:调(?:到|至|成|大|小|高|低)?|设置(?:为|成)?|开到|"
    r"大一点|小一点|大点|小点|静音|关掉|关闭)?(?:\s*\d{1,3}\s*%?)?[。！!]*$"
)
_MUSIC_PLAY_RE = re.compile(r"^(?:请|帮我)?(?:放|播放|来|听)(?:一)?(?:首|点|段)?(?:歌|音乐|歌曲)(?:听听|吧)?[。！!]*$")
_MUSIC_PAUSE_RE = re.compile(r"^(?:请|帮我)?(?:暂停|停止)(?:播放|音乐|歌曲)?[。！!]*$")
_MUSIC_NEXT_RE = re.compile(r"^(?:请|帮我)?(?:下一首|下一曲|切歌|换一首|换首歌|换下一首)[。！!]*$")
_LOCK_RE = re.compile(r"^(?:请|帮我)?(?:锁屏|锁定屏幕|锁一下屏)[。！!]*$")


_WAKE_PREFIXES = ("嘿，贾维斯", "嘿贾维斯", "贾维斯，", "贾维斯,", "贾维斯：", "贾维斯:", "贾维斯 ")


def strip_wake_word(text: str) -> tuple[str, bool]:
    """Split an optional wake word from the actual command.

    Returns (utterance, woke). The wake word itself is not part of the command, so
    "贾维斯，锁屏" must reach the fast-command matcher as "锁屏".
    """
    t = (text or "").strip()
    for prefix in _WAKE_PREFIXES:
        if t.startswith(prefix):
            return t[len(prefix):].strip(" ，,。！!：:"), True
    if t == "贾维斯":
        return "", True
    return t, False


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text.strip())


class SystemActionExecutor:
    """Executes macOS system control actions safely using structured subprocess calls."""

    @staticmethod
    def _run_safe(cmd_args: list[str], timeout: float = 3.0) -> str:
        try:
            p = subprocess.run(cmd_args, capture_output=True, text=True, timeout=timeout)
            return p.stdout.strip()
        except Exception as e:
            print(f"[Action] 执行系统命令异常 ({cmd_args}): {e}", flush=True)
            return ""

    @classmethod
    def execute_action(cls, action_str: str) -> None:
        """Execute a parsed action safely."""
        if not action_str:
            return
        action_str = action_str.strip()
        print(f"[*] 执行 Mac 动作: {action_str}", flush=True)

        # 1. Volume setting
        if action_str.startswith("set_volume:"):
            val_part = action_str.split(":", 1)[1].strip()
            try:
                vol = min(100, max(0, int(val_part)))
                cls._run_safe(["osascript", "-e", f"set volume output volume {vol}"])
            except ValueError:
                pass
        elif action_str == "volume_up":
            cls._run_safe([
                "osascript", "-e",
                "set volume output volume ((output volume of (get volume settings)) + 12)"
            ])
        elif action_str == "volume_down":
            cls._run_safe([
                "osascript", "-e",
                "set volume output volume ((output volume of (get volume settings)) - 12)"
            ])
        elif action_str == "mute":
            cls._run_safe(["osascript", "-e", "set volume output muted true"])
        elif action_str == "unmute":
            cls._run_safe(["osascript", "-e", "set volume output muted false"])

        # 2. Apple Music control
        elif action_str == "music_play":
            cls._run_safe(["osascript", "-e", 'tell application "Music" to play'])
        elif action_str == "music_pause":
            cls._run_safe(["osascript", "-e", 'tell application "Music" to pause'])
        elif action_str == "music_next":
            cls._run_safe(["osascript", "-e", 'tell application "Music" to next track'])

        # 3. Application opening (Strict regex check to prevent command injection)
        elif action_str.startswith("open_app:"):
            app = action_str.split(":", 1)[1].strip()
            if SAFE_APP_PATTERN.match(app):
                cls._run_safe(["open", "-a", app])
            else:
                print(f"[!] 拒绝执行非法应用名称: '{app}'", flush=True)

        # 4. Lock screen
        elif action_str == "lock_screen":
            # Ctrl+Cmd+Q works on every supported macOS version (CGSession was removed).
            cls._run_safe([
                "osascript", "-e",
                'tell application "System Events" to keystroke "q" using {control down, command down}'
            ])

    @classmethod
    def plan_fast_command(
        cls,
        text: str,
        token_service: Optional[Any] = None,
        weather_service: Optional[Any] = None,
    ) -> Optional[tuple[str, Optional[str]]]:
        """
        Pure matcher: returns (reply, action) without touching the system.

        Keeping planning separate from execution makes the router unit-testable and
        prevents a test (or a dry run) from changing the user's volume or locking the screen.
        """
        t = _normalize(text)
        if not t or len(t) > 24:
            return None

        if _TOKEN_RE.match(t):
            if token_service:
                return token_service.get_stats_text(), None
            return "当前未启用 Token 监控服务。", None

        if _WEATHER_RE.match(t):
            if weather_service:
                return weather_service.get_weather_text(), None
            return "当前未启用天气服务。", None

        if _TIME_RE.match(t):
            now = datetime.datetime.now()
            if "明天" in t:
                now += datetime.timedelta(days=1)
            weekday = ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]
            if "几号" in t or "几月" in t:
                return f"{'明天' if '明天' in t else '今天'}是 {now.month}月{now.day}日，星期{weekday}。", None
            if "星期" in t or "周几" in t:
                return f"{'明天' if '明天' in t else '今天'}是星期{weekday}。", None
            return f"现在是 {now.strftime('%H点%M分')}，星期{weekday}。", None

        if _VOLUME_RE.match(t):
            if any(k in t for k in ("静音", "关掉", "关闭")):
                return "已为您静音。", "mute"
            m = re.search(r"(\d{1,3})", t)
            if m:
                vol = min(100, max(0, int(m.group(1))))
                return f"音量已调至 {vol}%。", f"set_volume:{vol}"
            if any(k in t for k in ("大", "高", "升")):
                return "音量已调大。", "volume_up"
            if any(k in t for k in ("小", "低", "降")):
                return "音量已调小。", "volume_down"
            return "请说要调整到的音量，例如“音量调到 50”。", None

        if _MUSIC_PLAY_RE.match(t):
            return "已开始播放音乐。", "music_play"
        if _MUSIC_PAUSE_RE.match(t):
            return "音乐已暂停。", "music_pause"
        if _MUSIC_NEXT_RE.match(t):
            return "已为您切换到下一首。", "music_next"

        if _LOCK_RE.match(t):
            return "已为您锁定屏幕。", "lock_screen"

        return None

    @classmethod
    def match_fast_command(
        cls,
        text: str,
        token_service: Optional[Any] = None,
        weather_service: Optional[Any] = None,
        execute: bool = True,
    ) -> Optional[str]:
        """Match and (optionally) execute a fast local command."""
        plan = cls.plan_fast_command(text, token_service, weather_service)
        if plan is None:
            return None
        reply, action = plan
        if execute and action:
            cls.execute_action(action)
        return reply
