"""剪贴板与键盘全部模拟，不向真实光标输入任何内容。"""

from unittest.mock import Mock

import pytest

from jarvis_gateway.actions.injector import CursorInjector


@pytest.fixture
def clipboard(monkeypatch):
    state = {"value": b"original"}
    monkeypatch.setattr(CursorInjector, "_original_clipboard", None)
    monkeypatch.setattr(CursorInjector, "_injected_clipboard", None)
    monkeypatch.setattr(CursorInjector, "_possible_injected_clipboard", None)
    monkeypatch.setattr(CursorInjector, "_restore_timer", None)
    monkeypatch.setattr(CursorInjector, "_restore_generation", 0)
    monkeypatch.setattr(CursorInjector, "_restore_retries", 0)
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


@pytest.mark.parametrize("failure", ["format", "read", "write"])
def test_failed_second_injection_keeps_previous_restore(clipboard, monkeypatch, failure):
    assert CursorInjector.inject_text("第一段")
    timer = CursorInjector._restore_timer
    generation = CursorInjector._restore_generation
    with monkeypatch.context() as patch:
        method, result = {
            "format": ("_clipboard_has_text", None),
            "read": ("_read_clipboard", None),
            "write": ("_write_clipboard", False),
        }[failure]
        patch.setattr(CursorInjector, method, lambda *args: result)
        assert CursorInjector.inject_text("第二段")
    assert CursorInjector._restore_timer is timer
    timer.cancel.assert_not_called()
    assert CursorInjector._restore_generation == generation
    assert CursorInjector._original_clipboard == b"original"
    assert CursorInjector._injected_clipboard == "第一段".encode()
    CursorInjector._do_restore(generation)
    assert clipboard["value"] == b"original"


@pytest.mark.parametrize("failure", ["format", "read", "write"])
def test_temporary_restore_failure_retains_backup_and_retries(clipboard, monkeypatch, failure):
    assert CursorInjector.inject_text("第一段")
    generation = CursorInjector._restore_generation
    with monkeypatch.context() as patch:
        method, result = {
            "format": ("_clipboard_has_text", None),
            "read": ("_read_clipboard", None),
            "write": ("_write_clipboard", False),
        }[failure]
        patch.setattr(CursorInjector, method, lambda *args: result)
        CursorInjector._do_restore(generation)
    assert CursorInjector._original_clipboard == b"original"
    assert CursorInjector._restore_retries == 1
    assert CursorInjector._restore_timer is not None
    CursorInjector._do_restore(generation)
    assert clipboard["value"] == b"original"
    assert CursorInjector._original_clipboard is None


def test_restore_retry_is_bounded_without_discarding_backup(clipboard, monkeypatch):
    timer_factory = Mock()
    monkeypatch.setattr("jarvis_gateway.actions.injector.threading.Timer", timer_factory)
    assert CursorInjector.inject_text("第一段")
    monkeypatch.setattr(CursorInjector, "_write_clipboard", lambda data: False)
    generation = CursorInjector._restore_generation
    for _ in range(10):
        CursorInjector._do_restore(generation)
    assert timer_factory.call_count == 1 + CursorInjector._max_restore_retries
    assert CursorInjector._restore_timer is None
    assert CursorInjector._original_clipboard == b"original"
    assert clipboard["value"] == "第一段".encode()


def test_user_copy_during_retry_is_never_overwritten(clipboard, monkeypatch):
    assert CursorInjector.inject_text("第一段")
    generation = CursorInjector._restore_generation
    with monkeypatch.context() as patch:
        patch.setattr(CursorInjector, "_write_clipboard", lambda data: False)
        CursorInjector._do_restore(generation)
    clipboard["value"] = b"user-copy"
    write = Mock()
    monkeypatch.setattr(CursorInjector, "_write_clipboard", write)
    CursorInjector._do_restore(generation)
    write.assert_not_called()
    assert clipboard["value"] == b"user-copy"
    assert CursorInjector._original_clipboard is None


def test_new_nontext_copy_cancels_obsolete_restore(clipboard, monkeypatch):
    assert CursorInjector.inject_text("第一段")
    timer = CursorInjector._restore_timer
    clipboard["value"] = b"user-image"
    monkeypatch.setattr(CursorInjector, "_clipboard_has_text", lambda: False)
    assert CursorInjector.inject_text("第二段")
    timer.cancel.assert_called_once()
    CursorInjector._do_restore()
    assert clipboard["value"] == b"user-image"
    assert CursorInjector._original_clipboard is None


def test_failed_paste_with_failed_restore_retains_original(clipboard, monkeypatch):
    assert CursorInjector.inject_text("第一段")
    writer = CursorInjector._write_clipboard
    with monkeypatch.context() as patch:
        patch.setattr(CursorInjector, "_press_paste", lambda: False)
        patch.setattr(CursorInjector, "_write_clipboard",
                      lambda data: False if data == b"original" else writer(data))
        patch.setattr(CursorInjector, "_keystroke", lambda text: False)
        assert not CursorInjector.inject_text("第二段")
    assert CursorInjector._original_clipboard == b"original"
    assert CursorInjector._restore_timer is not None
    CursorInjector._do_restore()
    assert clipboard["value"] == b"original"


def test_clipboard_info_errors_are_not_proof_of_a_user_copy(monkeypatch):
    monkeypatch.setattr("jarvis_gateway.actions.injector.subprocess.run",
                        Mock(return_value=Mock(returncode=1, stdout="")))
    assert CursorInjector._clipboard_has_text() is None


def test_failed_write_that_changed_clipboard_still_restores_original(clipboard, monkeypatch):
    assert CursorInjector.inject_text("第一段")
    original_timer = CursorInjector._restore_timer

    def write_then_fail(value):
        clipboard["value"] = value
        return False

    with monkeypatch.context() as patch:
        patch.setattr(CursorInjector, "_write_clipboard", write_then_fail)
        assert CursorInjector.inject_text("第二段")
    original_timer.cancel.assert_not_called()
    CursorInjector._do_restore()
    assert clipboard["value"] == b"original"


def test_failed_first_write_keeps_backup_until_ownership_can_be_checked(clipboard, monkeypatch):
    def write_then_fail(value):
        clipboard["value"] = value
        return False

    with monkeypatch.context() as patch:
        patch.setattr(CursorInjector, "_write_clipboard", write_then_fail)
        assert CursorInjector.inject_text("第一段")
    assert CursorInjector._original_clipboard == b"original"
    CursorInjector._do_restore()
    assert clipboard["value"] == b"original"
