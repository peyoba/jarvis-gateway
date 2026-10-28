"""
双通道 LLM：端到端取消时限、逐请求结果及真实/估算用量。
同步包装供 HTTP 线程使用；异步客户端只活在本次请求的事件循环中。
"""

from __future__ import annotations

import asyncio
import copy
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Tuple

from openai import APITimeoutError, AsyncOpenAI

from jarvis_gateway.llm.prompts import CHAT_SYSTEM_PROMPT, POLISH_SYSTEM_PROMPT


@dataclass(frozen=True)
class LLMUsage:
    calls: int = 0
    total_tokens: int = 0
    estimated: bool = False
    incomplete: bool = False  # 失败/超时请求没有账单，不能宣称其消耗为零。


@dataclass(frozen=True)
class PolishResult:
    text: str
    polished: bool = False
    reason: str = ""
    usage: LLMUsage = LLMUsage()
    channel: str = ""


@dataclass(frozen=True)
class ChatResult:
    reply: str
    actions: Tuple[str, ...] = ()
    success: bool = True
    error: str = ""
    usage: LLMUsage = LLMUsage()
    channel: str = ""


@dataclass
class _UsageCounter:
    """仅本次调用可变，不在请求之间共享。"""

    calls: int = 0
    total_tokens: int = 0
    estimated: bool = False
    incomplete: bool = False

    def record(self, response, messages, text):
        tokens = getattr(getattr(response, "usage", None), "total_tokens", None)
        if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
            self.total_tokens += tokens
        else:
            # 仅对确实返回了响应的模型调用估算，原字/短句/无模型不计费。
            self.total_tokens += 2 * (sum(len(m["content"]) for m in messages) + len(text))
            self.estimated = True

    def snapshot(self) -> LLMUsage:
        return LLMUsage(self.calls, self.total_tokens, self.estimated, self.incomplete)


class LLMClientManager:
    def __init__(
        self,
        llm_config: Dict[str, Any],
        *,
        client_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = copy.deepcopy(llm_config)
        self.primary_cfg = self.config.get("primary", {})
        self.fallback_cfg = self.config.get("fallback", {})
        self._client_factory = client_factory or AsyncOpenAI
        self._clock = clock
        self._compat = threading.local()

    @property
    def last_chat_failed(self) -> bool:
        """旧 chat() 调用者的线程局部兼容标记；生产路由使用 ChatResult。"""
        return getattr(self._compat, "last_chat_failed", False)

    def _channels(self):
        return [
            (name, cfg) for name, cfg in (
                ("primary", self.primary_cfg), ("fallback", self.fallback_cfg),
            ) if cfg.get("api_key") and cfg.get("base_url") and cfg.get("model")
        ]

    async def _call_channel(self, cfg, messages, parameters, timeout, usage):
        # 不能跨 asyncio.run 复用连接池；上下文退出（包括取消）会关闭真实 I/O。
        async with self._client_factory(
            api_key=cfg["api_key"], base_url=cfg["base_url"],
            timeout=timeout, max_retries=0,
        ) as client:
            usage.calls += 1
            try:
                response = await client.chat.completions.create(
                    model=cfg["model"], messages=messages, timeout=timeout, **parameters,
                )
                text = response.choices[0].message.content or ""
                if not isinstance(text, str):
                    raise ValueError("unsupported model response")
                usage.record(response, messages, text)
                return text.strip()
            except BaseException:
                usage.incomplete = True
                raise

    async def _try_channels(self, channels, messages, parameters, deadline, usage, normalize):
        error = "llm_unavailable"
        for name, cfg in channels:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return "", "", "llm_timeout"
            timeout = min(float(cfg.get("timeout", 8)), remaining)
            try:
                # SDK timeout 只限制每次网络操作；wait_for 包含本通道的整个调用。
                text = await asyncio.wait_for(
                    self._call_channel(cfg, messages, parameters, timeout, usage), timeout,
                )
                if self._clock() >= deadline:
                    return "", "", "llm_timeout"
                if normalize is not None:
                    text = normalize(text)
                if text:
                    return text, name, ""
                error = "llm_empty_response"
            except Exception as exc:
                error = "llm_timeout" if isinstance(exc, (asyncio.TimeoutError, APITimeoutError)) else "llm_unavailable"
                # 不输出供应商错误正文、地址、令牌、模型响应或用户文本。
                print(f"[!] LLM 通道 {name} 失败: {type(exc).__name__}", flush=True)
        return "", "", error

    async def _complete(self, messages, *, normalize=None, **parameters):
        usage = _UsageCounter()
        channels = self._channels()
        if not channels:
            return "", "", "llm_unconfigured", usage.snapshot()
        budget = float(self.config.get("total_timeout", 16))
        if budget <= 0:
            return "", "", "llm_timeout", usage.snapshot()
        deadline = self._clock() + budget
        try:
            # 绝对总时限覆盖主/备通道；超时会取消并等待清理，不留下后台请求。
            text, channel, error = await asyncio.wait_for(
                self._try_channels(channels, messages, parameters, deadline, usage, normalize), budget,
            )
        except asyncio.TimeoutError:
            text, channel, error = "", "", "llm_timeout"
        return text, channel, error, usage.snapshot()

    @staticmethod
    def _normalize_polish(text):
        if (text.startswith('"') and text.endswith('"')) or (
            text.startswith("“") and text.endswith("”")
        ):
            return text[1:-1].strip()
        return text

    async def apolish_text_result(self, raw_text: str) -> PolishResult:
        text = raw_text.strip()
        if len(text) <= 3:
            return PolishResult(text, reason="short_text")
        polished, channel, error, usage = await self._complete(
            [
                {"role": "system", "content": POLISH_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            max_tokens=1024, temperature=0.2, normalize=self._normalize_polish,
        )
        if not polished:
            return PolishResult(text, reason=error or "llm_empty_response", usage=usage)
        return PolishResult(polished, polished=True, usage=usage, channel=channel)

    def polish_text_result(self, raw_text: str) -> PolishResult:
        return asyncio.run(self.apolish_text_result(raw_text))

    def polish_text(self, raw_text: str) -> str:
        """保留原有字符串接口；需要降级原因/用量时使用 polish_text_result。"""
        return self.polish_text_result(raw_text).text

    async def achat_result(self, user_text: str) -> ChatResult:
        raw_reply, channel, error, usage = await self._complete(
            [
                {"role": "system", "content": CHAT_SYSTEM_PROMPT},
                {"role": "user", "content": user_text or "你好"},
            ],
            max_tokens=60,
        )
        if not raw_reply:
            return ChatResult(
                "连接神经中枢出现波动，请稍后再试。", success=False,
                error=error, usage=usage,
            )
        actions = tuple(re.findall(r"\[ACTION:(.*?)\]", raw_reply))
        clean_reply = re.sub(r"\[ACTION:.*?\]", "", raw_reply).strip()
        return ChatResult(clean_reply, actions, usage=usage, channel=channel)

    def chat_result(self, user_text: str) -> ChatResult:
        return asyncio.run(self.achat_result(user_text))

    def chat(self, user_text: str) -> Tuple[str, List[str]]:
        """保留原有 (回复, 动作列表) 接口，不供生产路由推断执行状态。"""
        result = self.chat_result(user_text)
        self._compat.last_chat_failed = not result.success
        return result.reply, list(result.actions)
