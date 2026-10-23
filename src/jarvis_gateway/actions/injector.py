"""
Thread-safe Mac Cursor Typing Injector with Atomic Clipboard Preservation.
"""

from __future__ import annotations

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
            return any(k in info for k in ("«class utf8»", "«class text»", "unicode text", "string"))
        except Exception:
            return False

    @classmethod
    def _read_clipboard(cls) -> bytes:
        try:
            p = subprocess.Popen(["pbpaste"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            out, _ = p.communicate(timeout=1.0)
            return out
        except Exception:
            return b""

    @classmethod
    def _write_clipboard(cls, data: bytes) -> bool:
        try:
            p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            p.communicate(data, timeout=1.0)
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
    def _do_restore(cls):
        with cls._lock:
            if cls._original_clipboard is not None:
                cls._write_clipboard(cls._original_clipboard)
                cls._original_clipboard = None
            cls._restore_timer = None

    @classmethod
    def inject_text(cls, text: str, restore_delay_sec: float = 0.18) -> bool:
        if not text:
            return False
        clean_text = text.strip()
        if not clean_text:
            return False

        print(
            f"[⌨️ 打字注入] 开始向 Mac 当前光标注入文本 ({len(clean_text)}字): '{clean_text}'",
            flush=True,
        )

        with cls._lock:
            if not cls._clipboard_has_text():
                # Preserve non-text clipboard contents (images/files) by bypassing it.
                ok = cls._keystroke(clean_text)
                print(f"[⌨️ 打字注入] 剪贴板非文本，改用直接键入: {'成功' if ok else '失败'}", flush=True)
                return ok

            if cls._restore_timer is not None:
                cls._restore_timer.cancel()
                cls._restore_timer = None
            else:
                cls._original_clipboard = cls._read_clipboard()

            if not cls._write_clipboard(clean_text.encode("utf-8")):
                cls._original_clipboard = None
                return cls._keystroke(clean_text)

            if not cls._press_paste():
                # Paste failed: restore immediately and type the text directly.
                if cls._original_clipboard is not None:
                    cls._write_clipboard(cls._original_clipboard)
                    cls._original_clipboard = None
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
                cls._restore_timer = threading.Timer(restore_delay_sec, cls._do_restore)
                cls._restore_timer.daemon = True
                cls._restore_timer.start()

        return True
