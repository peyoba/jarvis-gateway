"""
Thread-safe Mac Cursor Typing Injector with Atomic Clipboard Preservation.
"""

from __future__ import annotations

import re
import subprocess
import threading
from typing import Optional


class CursorInjector:
    """
    Injects text into the focused Mac app using clipboard + Cmd+V.

    The clipboard is restored after pasting. If the clipboard holds a non-text
    payload (image, file, rich data), we never touch it: we type via AppleScript
    keystrokes instead, so the user's copied image is not destroyed.
    """

    _lock = threading.Lock()
    _original_clipboard: Optional[bytes] = None
    _restore_timer: Optional[threading.Timer] = None
    _injected_clipboard: Optional[bytes] = None
    _possible_injected_clipboard: Optional[bytes] = None
    _restore_generation = 0
    _restore_retries = 0
    _max_restore_retries = 3
    _retry_delay_sec = 0.2

    @classmethod
    def _clipboard_has_text(cls) -> Optional[bool]:
        """None 表示读取失败/格式未知，不能当作用户已换掉剪贴板。"""
        try:
            p = subprocess.run(
                ["osascript", "-e", "clipboard info"],
                capture_output=True,
                text=True,
                timeout=1.0,
            )
            info = (p.stdout or "").lower()
            # 富文本/图片/文件及未知格式走直接键入，不降级为纯文本备份。
            formats = re.findall(r"«class ([^»]+)»", info)
            if p.returncode:
                return None
            if any(f not in {"utf8", "ut16", "utxt", "text"} for f in formats):
                return False
            if any(f in info for f in ("rtf", "html", "picture", "file")):
                return False
            if formats or any(k in info for k in ("unicode text", "string")):
                return True
            return None
        except Exception:
            return None

    @classmethod
    def _read_clipboard(cls) -> Optional[bytes]:
        try:
            p = subprocess.run(["pbpaste"], capture_output=True, timeout=1.0, check=True)
            return p.stdout
        except Exception:
            return None

    @classmethod
    def _write_clipboard(cls, data: bytes) -> bool:
        try:
            p = subprocess.run(["pbcopy"], input=data, capture_output=True, timeout=1.0)
            return p.returncode == 0
        except Exception:
            return False

    @classmethod
    def _keystroke(cls, text: str) -> bool:
        """Type directly through System Events (no clipboard modification)."""
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        try:
            p = subprocess.run(
                ["osascript", "-e", f'tell application "System Events" to keystroke "{escaped}"'],
                capture_output=True,
                timeout=30.0,
            )
            return p.returncode == 0
        except Exception:
            return False

    @classmethod
    def _press_paste(cls) -> bool:
        try:
            p = subprocess.run(
                ["osascript", "-e", 'tell application "System Events" to key code 9 using command down'],
                capture_output=True,
                timeout=2.0,
            )
            return p.returncode == 0
        except Exception:
            return False

    @classmethod
    def _cancel_restore(cls):
        cls._restore_generation += 1
        if cls._restore_timer is not None:
            cls._restore_timer.cancel()
            cls._restore_timer = None
        cls._restore_retries = 0

    @classmethod
    def _clear_backup(cls):
        cls._cancel_restore()
        cls._original_clipboard = None
        cls._injected_clipboard = None
        cls._possible_injected_clipboard = None

    @classmethod
    def _schedule_restore(cls, delay):
        cls._restore_timer = threading.Timer(
            delay, cls._do_restore, args=(cls._restore_generation,)
        )
        cls._restore_timer.daemon = True
        cls._restore_timer.start()

    @classmethod
    def _resume_restore(cls, delay):
        if cls._original_clipboard is not None and cls._restore_timer is None:
            cls._cancel_restore()
            cls._schedule_restore(delay)

    @classmethod
    def _restore_if_owned(cls) -> bool:
        """已恢复/确定失去所有权返回 True；临时失败返回 False 并保留备份。"""
        if cls._original_clipboard is None or cls._injected_clipboard is None:
            return True
        has_text = cls._clipboard_has_text()
        if has_text is None:
            return False
        if has_text is False:
            cls._clear_backup()  # 用户新复制了图片/富文本/文件，绝不能覆盖。
            return True
        current = cls._read_clipboard()
        if current is None:
            return False
        if current == cls._original_clipboard:
            cls._clear_backup()
            return True
        if current not in (cls._injected_clipboard, cls._possible_injected_clipboard):
            cls._clear_backup()
            return True
        if not cls._write_clipboard(cls._original_clipboard):
            return False
        cls._clear_backup()
        return True

    @classmethod
    def _do_restore(cls, generation=None):
        with cls._lock:
            if generation is not None and generation != cls._restore_generation:
                return
            cls._restore_timer = None
            if cls._restore_if_owned():
                return
            if cls._restore_retries < cls._max_restore_retries:
                cls._restore_retries += 1
                cls._schedule_restore(cls._retry_delay_sec)
            else:
                # 有界重试；备份继续保留，后续注入可以重新尝试，不能盲目覆盖。
                print("[⌨️ 打字注入] 剪贴板恢复暂时失败，已保留备份", flush=True)

    @classmethod
    def inject_text(cls, text: str, restore_delay_sec: float = 0.18) -> bool:
        if not text:
            return False
        clean_text = text.strip()
        if not clean_text:
            return False

        print(
            f"[⌨️ 打字注入] 开始注入 ({len(clean_text)}字)",
            flush=True,
        )

        with cls._lock:
            has_text = cls._clipboard_has_text()
            if has_text is not True:
                if has_text is False:
                    cls._clear_backup()
                else:
                    cls._resume_restore(restore_delay_sec)
                # Preserve non-text clipboard contents (images/files) by bypassing it.
                ok = cls._keystroke(clean_text)
                print(f"[⌨️ 打字注入] 剪贴板非纯文本或暂不可读，改用直接键入: {'成功' if ok else '失败'}", flush=True)
                return ok

            current = cls._read_clipboard()
            if current is None:
                # 备份失败不能等同空剪贴板，更不能覆盖未知的用户内容。
                cls._resume_restore(restore_delay_sec)
                return cls._keystroke(clean_text)
            original = (
                cls._original_clipboard
                if cls._original_clipboard is not None
                and current in (cls._injected_clipboard, cls._possible_injected_clipboard)
                else current
            )

            injected = clean_text.encode("utf-8")
            if not cls._write_clipboard(injected):
                # pbcopy 可能已改写粘贴板才返回失败。保存两种可能的自有值，
                # 后续仍须读取匹配才能恢复；用户新复制的不同内容绝不覆盖。
                cls._original_clipboard = original
                cls._injected_clipboard = current
                cls._possible_injected_clipboard = injected
                cls._resume_restore(restore_delay_sec)
                return cls._keystroke(clean_text)
            # 替换成功才提交新所有权并取消旧恢复任务；失败不能丢掉上一段的备份。
            cls._cancel_restore()
            cls._original_clipboard = original
            cls._injected_clipboard = injected
            cls._possible_injected_clipboard = None

            if not cls._press_paste():
                # Paste failed: restore immediately and type the text directly.
                if not cls._restore_if_owned():
                    cls._resume_restore(restore_delay_sec)
                return cls._keystroke(clean_text)

            try:
                subprocess.Popen(
                    ["afplay", "/System/Library/Sounds/Tink.aiff"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

            if cls._original_clipboard is not None:
                cls._schedule_restore(restore_delay_sec)

        return True
