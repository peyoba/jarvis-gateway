"""剪贴板与键盘全部模拟，不向真实光标输入任何内容。"""

from unittest.mock import Mock

import pytest

from jarvis_gateway.actions.injector import CursorInjector


@pytest.fixture
def clipboard(monkeypatch):
    state = {"value": b"original"}
    monkeypatch.setattr(CursorInjector, "_original_clipboard", None)
    monkeypatch.setattr(CursorInjector, "_injected_clipboard", None)
    monkeypatch.setattr(CursorInjector, "_restore_timer", None)
    monkeypatch.setattr(CursorInjector, "_restore_generation", 0)
    monkeypatch.setattr(CursorInjector, "_clipboard_has_text", lambda: True)
    monkeypatch.setattr(CursorInjector, "_read_clipboard", lambda: state["value"])
    monkeypatch.setattr(CursorInjector, "_press_paste", lambda: True)
    monkeypatch.setattr(CursorInjector, "_keystroke", Mock(return_value=True))

    def write(value):
        state["value"] = value
        return True

    monkeypatch.setattr(CursorInjector, "_write_clipboard", write)
    monkeypatch.setattr("jarvis_gateway.actions.injector.subprocess.Popen", Mock())
    monkeypatch.setattr("jarvis_gateway.actions.injector.threading.Timer", Mock())
    return state


def test_restore_keeps_new_copy(clipboard):
    assert CursorInjector.inject_text("听写")
    clipboard["value"] = b"new-copy"
    CursorInjector._do_restore()
    assert clipboard["value"] == b"new-copy"


def test_continuous_injection_restores_original(clipboard):
    assert CursorInjector.inject_text("第一段")
    assert CursorInjector.inject_text("第二段")
    CursorInjector._do_restore()
    assert clipboard["value"] == b"original"


def test_stale_timer_does_not_restore_current_injection(clipboard):
    assert CursorInjector.inject_text("第一段")
    old = CursorInjector._restore_generation
    assert CursorInjector.inject_text("第二段")
    CursorInjector._do_restore(old)
    assert clipboard["value"] == "第二段".encode()
    CursorInjector._do_restore(CursorInjector._restore_generation)
    assert clipboard["value"] == b"original"


def test_failed_backup_does_not_overwrite_clipboard(clipboard, monkeypatch):
    monkeypatch.setattr(CursorInjector, "_read_clipboard", lambda: None)
    assert CursorInjector.inject_text("听写")
    assert clipboard["value"] == b"original"
    CursorInjector._keystroke.assert_called_once_with("听写")


def test_non_text_clipboard_uses_direct_typing(clipboard, monkeypatch):
    monkeypatch.setattr(CursorInjector, "_clipboard_has_text", lambda: False)
    assert CursorInjector.inject_text("听写")
    assert clipboard["value"] == b"original"
    CursorInjector._keystroke.assert_called_once()


def test_failed_paste_restores_before_fallback(clipboard, monkeypatch):
    monkeypatch.setattr(CursorInjector, "_press_paste", lambda: False)
    monkeypatch.setattr(CursorInjector, "_keystroke", Mock(return_value=False))
    assert not CursorInjector.inject_text("听写")
    assert clipboard["value"] == b"original"
