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
    _restore_generation = 0

    @classmethod
    def _clipboard_has_text(cls) -> bool:
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
            if p.returncode or any(f not in {"utf8", "ut16", "utxt", "text"} for f in formats):
                return False
            if any(f in info for f in ("rtf", "html", "picture", "file")):
                return False
            return any(k in info for k in ("«class utf8»", "«class text»", "unicode text", "string"))
        except Exception:
            return False

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
    def _restore_if_owned(cls):
        if (
            cls._original_clipboard is not None
            and cls._injected_clipboard is not None
            and cls._clipboard_has_text()
            and cls._read_clipboard() == cls._injected_clipboard
        ):
            cls._write_clipboard(cls._original_clipboard)
        cls._original_clipboard = None
        cls._injected_clipboard = None

    @classmethod
    def _do_restore(cls, generation=None):
        with cls._lock:
            if generation is not None and generation != cls._restore_generation:
                return
            cls._restore_if_owned()
            cls._restore_timer = None

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
            cls._restore_generation += 1
            if cls._restore_timer is not None:
                cls._restore_timer.cancel()
                cls._restore_timer = None
            if not cls._clipboard_has_text():
                cls._original_clipboard = None
                cls._injected_clipboard = None
                # Preserve non-text clipboard contents (images/files) by bypassing it.
                ok = cls._keystroke(clean_text)
                print(f"[⌨️ 打字注入] 剪贴板非文本，改用直接键入: {'成功' if ok else '失败'}", flush=True)
                return ok

            current = cls._read_clipboard()
            if current is None:
                # 备份失败不能等同空剪贴板，更不能覆盖未知的用户内容。
                cls._original_clipboard = None
                cls._injected_clipboard = None
                return cls._keystroke(clean_text)
            if cls._original_clipboard is None or current != cls._injected_clipboard:
                cls._original_clipboard = current

            injected = clean_text.encode("utf-8")
            if not cls._write_clipboard(injected):
                cls._original_clipboard = None
                cls._injected_clipboard = None
                return cls._keystroke(clean_text)
            cls._injected_clipboard = injected

            if not cls._press_paste():
                # Paste failed: restore immediately and type the text directly.
                cls._restore_if_owned()
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
                cls._restore_timer = threading.Timer(
                    restore_delay_sec, cls._do_restore, args=(cls._restore_generation,)
                )
                cls._restore_timer.daemon = True
                cls._restore_timer.start()

        return True
