"""双通道取消、请求结果与用量回归；本文件不使用真实网络。"""

import asyncio
import time
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jarvis_gateway.llm.client import LLMClientManager, LLMUsage


class _FakeClient:
    def __init__(self, reply=None, delay=0, tokens=12):
        self.reply, self.delay, self.tokens = reply, delay, tokens
        self.calls = []
        self.closed = False
        self.cancelled = False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            reply = await self.reply(**kwargs) if callable(self.reply) else self.reply
            if reply is None:
                raise RuntimeError("synthetic-private-provider-error")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
                usage=SimpleNamespace(total_tokens=self.tokens) if self.tokens is not None else None,
            )
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _FakeFactory:
    def __init__(self, primary, fallback):
        self.plans = {"primary": primary, "fallback": fallback}
        self.clients, self.calls = [], []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        channel = "primary" if "primary" in kwargs["base_url"] else "fallback"
        client = _FakeClient(**self.plans[channel])
        self.clients.append(client)
        return client


def _manager(primary_reply=None, fallback_reply=None, **options):
    factory = _FakeFactory(
        {"reply": primary_reply, **options.get("primary", {})},
        {"reply": fallback_reply, **options.get("fallback", {})},
    )
    cfg = {"total_timeout": 3.0}
    for name, timeout in (("primary", 1.0), ("fallback", 2.0)):
        cfg[name] = {
            "api_key": "synthetic-test-only", "base_url": f"https://{name}.invalid/v1",
            "model": f"{name}-model", "timeout": timeout,
        }
    return LLMClientManager(cfg, client_factory=factory), factory


def test_chat_falls_back_and_parses_actions():
    manager, factory = _manager(fallback_reply="好的。[ACTION:volume_up]")
    result = manager.chat_result("大点声")
    assert result.reply == "好的。"
    assert result.actions == ("volume_up",)
    assert result.success and result.channel == "fallback"
    assert result.usage == LLMUsage(calls=2, total_tokens=12, incomplete=True)
    assert factory.clients[1].calls[0]["timeout"] == 2.0
    assert all(client.closed for client in factory.clients)
    assert manager.chat("大点声") == ("好的。", ["volume_up"])


def test_all_channel_failures_do_not_echo_provider_errors(capsys):
    manager, factory = _manager()
    result = manager.chat_result("你好")
    assert "波动" in result.reply and result.actions == ()
    assert not result.success and result.error == "llm_unavailable"
    assert result.usage.calls == 2 and result.usage.total_tokens == 0
    assert result.usage.incomplete
    assert all(client.closed for client in factory.clients)
    manager.chat("你好")
    assert manager.last_chat_failed
    assert "synthetic-private-provider-error" not in capsys.readouterr().out


def test_polish_falls_back_and_strips_wrapping_quotes():
    manager, factory = _manager(fallback_reply="“这是润色后的文本。”")
    result = manager.polish_text_result("这是润色后的文本")
    assert result.text == "这是润色后的文本。"
    assert result.polished and not result.reason
    assert result.usage.total_tokens == 12 and result.usage.calls == 2
    assert all(client.closed for client in factory.clients)
    assert manager.polish_text("这是润色后的文本") == result.text


def test_empty_primary_response_usage_is_counted_before_fallback():
    manager, _ = _manager(primary_reply="", fallback_reply="备用回答",
                          primary={"tokens": 5}, fallback={"tokens": 8})
    result = manager.chat_result("测试")
    assert result.success and result.usage == LLMUsage(calls=2, total_tokens=13)


@pytest.mark.parametrize("empty", ['""', "“ ”"])
def test_quote_only_polish_response_still_falls_back(empty):
    manager, _ = _manager(primary_reply=empty, fallback_reply="有意义的润色文本")
    result = manager.polish_text_result("需要润色的口述文本")
    assert result.polished and result.text == "有意义的润色文本"
    assert result.usage.calls == 2


def test_missing_provider_usage_is_explicitly_estimated():
    manager, _ = _manager(primary_reply="回复", primary={"tokens": None})
    result = manager.chat_result("测试")
    assert result.success and result.usage.calls == 1
    assert result.usage.estimated and not result.usage.incomplete
    assert result.usage.total_tokens > len(result.reply) * 2


def test_unconfigured_models_make_no_calls_or_fake_token_usage():
    factory = Mock()
    manager = LLMClientManager({"primary": {}, "fallback": {}}, client_factory=factory)
    result = manager.polish_text_result("这段原文应该直接保留")
    assert result.text == "这段原文应该直接保留"
    assert not result.polished and result.reason == "llm_unconfigured"
    assert result.usage == LLMUsage()
    assert manager.chat_result("测试").usage == LLMUsage()
    factory.assert_not_called()


@pytest.mark.parametrize("text", ["好", "你好", "  三个字  ", " "])
def test_short_text_skips_ai_without_token_usage(text):
    manager, factory = _manager(primary_reply="不应该调用")
    result = manager.polish_text_result(text)
    assert result.text == text.strip() and result.reason == "short_text"
    assert not result.polished and result.usage == LLMUsage()
    assert factory.calls == []


def test_sdk_retries_are_disabled_and_clients_are_request_scoped():
    manager, factory = _manager(fallback_reply="备用回复")
    assert factory.calls == []  # 无请求时不创建连接池。
    manager.chat_result("测试")
    assert len(factory.calls) == 2
    assert all(call["max_retries"] == 0 for call in factory.calls)
    assert all(client.closed for client in factory.clients)


def test_fallback_uses_remaining_deadline_without_patching_asyncio_clock():
    clock = [100.0]

    async def slow_primary(**kwargs):
        clock[0] += 2.5
        raise TimeoutError

    manager, factory = _manager(primary_reply=slow_primary, fallback_reply="备用回复")
    manager._clock = lambda: clock[0]
    result = manager.chat_result("测试")
    assert result.reply == "备用回复"
    assert factory.clients[1].calls[0]["timeout"] == 0.5


def test_exhausted_deadline_skips_fallback():
    clock = [100.0]

    async def slow_primary(**kwargs):
        clock[0] += 2
        raise TimeoutError

    manager, factory = _manager(primary_reply=slow_primary, fallback_reply="不应调用")
    manager.config["total_timeout"] = 1.0
    manager._clock = lambda: clock[0]
    result = manager.chat_result("测试")
    assert not result.success and result.error == "llm_timeout"
    assert len(factory.clients) == 1


def test_channel_timeout_cancels_and_closes_before_fallback():
    manager, factory = _manager(primary_reply="太晚", fallback_reply="备用回复",
                                primary={"delay": 1})
    manager.primary_cfg["timeout"] = 0.02
    result = manager.chat_result("测试")
    assert result.success and result.reply == "备用回复"
    assert factory.clients[0].cancelled
    assert all(client.closed for client in factory.clients)


@pytest.mark.parametrize("method", ["chat_result", "polish_text_result"])
def test_total_deadline_cancels_instead_of_leaving_background_requests(method):
    manager, factory = _manager(primary_reply="太晚", primary={"delay": 1})
    manager.config["total_timeout"] = 0.06
    started = time.monotonic()
    result = getattr(manager, method)("足够长的测试文字")
    assert time.monotonic() - started < 0.3
    assert getattr(result, "error", getattr(result, "reason", None)) == "llm_timeout"
    assert len(factory.clients) == 1 and factory.clients[0].cancelled
    assert factory.clients[0].closed
    assert result.usage.calls == 1 and result.usage.incomplete


def test_repeated_sync_requests_do_not_reuse_a_closed_event_loop_client():
    manager, factory = _manager(primary_reply="正常回复")
    first = manager.chat_result("第一段")
    second = manager.chat_result("第二段")
    assert first.success and second.success
    assert len(factory.clients) == 2
    assert factory.clients[0] is not factory.clients[1]
    assert all(client.closed for client in factory.clients)
    with pytest.raises(FrozenInstanceError):
        first.reply = "不能修改"
    with pytest.raises(FrozenInstanceError):
        first.usage.calls = 100


def test_parallel_async_results_do_not_share_last_failure_or_usage():
    async def reply(**kwargs):
        if kwargs["messages"][-1]["content"] == "失败":
            await asyncio.sleep(0.01)
            raise RuntimeError
        await asyncio.sleep(0.02)
        return "正常回复"

    manager, factory = _manager(primary_reply=reply)
    manager.fallback_cfg = {}

    async def run():
        return await asyncio.gather(manager.achat_result("成功"), manager.achat_result("失败"))

    success, failed = asyncio.run(run())
    assert success.success and not failed.success
    assert success.usage == LLMUsage(calls=1, total_tokens=12)
    assert failed.usage == LLMUsage(calls=1, incomplete=True)
    assert all(client.closed for client in factory.clients)


def test_client_initialization_failure_does_not_count_as_a_model_call():
    manager, _ = _manager()
    manager._client_factory = Mock(side_effect=OSError("synthetic-private-config"))
    result = manager.chat_result("测试")
    assert not result.success and result.usage == LLMUsage()
