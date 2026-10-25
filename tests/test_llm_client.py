"""Unit tests for dual-channel LLM fallback without network access."""

from types import SimpleNamespace

from jarvis_gateway.llm.client import LLMClientManager


class _FakeCompletions:
    def __init__(self, reply=None):
        self.reply = reply
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.reply is None:
            raise RuntimeError("provider unavailable")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))]
        )


class _FakeClient:
    def __init__(self, reply=None):
        self.chat = SimpleNamespace(completions=_FakeCompletions(reply))


def _manager(primary_reply=None, fallback_reply=None):
    manager = LLMClientManager({"primary": {}, "fallback": {}})
    manager.primary_cfg = {"model": "primary-model", "name": "Primary", "timeout": 1.0}
    manager.fallback_cfg = {"model": "fallback-model", "name": "Fallback", "timeout": 2.0}
    manager.primary_client = _FakeClient(primary_reply)
    manager.fallback_client = _FakeClient(fallback_reply)
    return manager


def test_chat_falls_back_and_parses_actions():
    manager = _manager(primary_reply=None, fallback_reply="好的。[ACTION:volume_up]")

    reply, actions = manager.chat("大点声")

    assert reply == "好的。"
    assert actions == ["volume_up"]
    assert manager.fallback_client.chat.completions.calls[0]["timeout"] == 2.0


def test_chat_reports_when_all_channels_fail():
    manager = _manager(primary_reply=None, fallback_reply=None)

    reply, actions = manager.chat("你好")

    assert "波动" in reply
    assert actions == []


def test_polish_falls_back_to_secondary_channel():
    manager = _manager(primary_reply=None, fallback_reply="这是润色后的文本。")

    assert manager.polish_text("这是润色后的文本") == "这是润色后的文本。"


def test_sdk_retries_are_disabled(monkeypatch):
    from unittest.mock import Mock

    factory = Mock()
    monkeypatch.setattr("jarvis_gateway.llm.client.OpenAI", factory)
    LLMClientManager({
        "primary": {"api_key": "test-only", "base_url": "https://primary.invalid"},
        "fallback": {"api_key": "test-only", "base_url": "https://fallback.invalid"},
    })
    assert factory.call_count == 2
    assert all(call.kwargs["max_retries"] == 0 for call in factory.call_args_list)


def test_fallback_uses_remaining_deadline(monkeypatch):
    manager = _manager(fallback_reply="备用回复")
    manager.config["total_timeout"] = 3.0
    clock = [100.0]
    monkeypatch.setattr("jarvis_gateway.llm.client.time.monotonic", lambda: clock[0])

    def slow_primary(**kwargs):
        clock[0] += 2.5
        raise TimeoutError

    manager.primary_client.chat.completions.create = slow_primary
    assert manager.chat("测试")[0] == "备用回复"
    assert manager.fallback_client.chat.completions.calls[0]["timeout"] == 0.5


def test_exhausted_deadline_skips_fallback(monkeypatch):
    manager = _manager(fallback_reply="不应调用")
    manager.config["total_timeout"] = 1.0
    clock = [100.0]
    monkeypatch.setattr("jarvis_gateway.llm.client.time.monotonic", lambda: clock[0])

    def slow_primary(**kwargs):
        clock[0] += 2
        raise TimeoutError

    manager.primary_client.chat.completions.create = slow_primary
    manager.chat("测试")
    assert manager.last_chat_failed is True
    assert manager.fallback_client.chat.completions.calls == []
