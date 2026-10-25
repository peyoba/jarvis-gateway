"""
LLM Client Manager supporting two OpenAI-compatible API channels.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Tuple

from openai import OpenAI

from jarvis_gateway.llm.prompts import CHAT_SYSTEM_PROMPT, POLISH_SYSTEM_PROMPT


class LLMClientManager:
    def __init__(self, llm_config: Dict[str, Any]):
        self.config = llm_config
        self.primary_cfg = llm_config.get("primary", {})
        self.fallback_cfg = llm_config.get("fallback", {})

        self.primary_client: OpenAI | None = None
        self.fallback_client: OpenAI | None = None
        self.last_chat_failed = False

        self._init_clients()

    def _init_clients(self):
        # Primary channel
        p_key = self.primary_cfg.get("api_key", "")
        p_url = self.primary_cfg.get("base_url", "")
        p_to = float(self.primary_cfg.get("timeout", 6.5))
        if p_key and p_url:
            try:
                self.primary_client = OpenAI(api_key=p_key, base_url=p_url, timeout=p_to, max_retries=0)
            except Exception as e:
                print(f"[!] 主模型客户端初始化失败: {type(e).__name__}", flush=True)

        # Fallback channel
        f_key = self.fallback_cfg.get("api_key", "")
        f_url = self.fallback_cfg.get("base_url", "")
        f_to = float(self.fallback_cfg.get("timeout", 3.5))
        if f_key and f_url:
            try:
                self.fallback_client = OpenAI(api_key=f_key, base_url=f_url, timeout=f_to, max_retries=0)
            except Exception as e:
                print(f"[!] 备用模型客户端初始化失败: {type(e).__name__}", flush=True)

    def _channels(self):
        """Return configured (client, model, label, timeout) channels in priority order."""
        channels = []
        if self.primary_client:
            channels.append((
                self.primary_client,
                self.primary_cfg.get("model", ""),
                self.primary_cfg.get("name", "Primary"),
                float(self.primary_cfg.get("timeout", 6.5)),
            ))
        if self.fallback_client:
            channels.append((
                self.fallback_client,
                self.fallback_cfg.get("model", ""),
                self.fallback_cfg.get("name", "Fallback"),
                float(self.fallback_cfg.get("timeout", 3.5)),
            ))
        return channels

    def polish_text(self, raw_text: str) -> str:
        """Polish spoken speech into formatted written text using lightweight LLM."""
        t0 = time.time()
        deadline = time.monotonic() + float(self.config.get("total_timeout", 16))
        t = raw_text.strip()
        if len(t) <= 3:
            return t

        for client, model_name, tag, timeout_sec in self._channels():
            timeout_sec = min(timeout_sec, deadline - time.monotonic())
            if timeout_sec <= 0:
                break
            if not model_name:
                continue
            try:
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": POLISH_SYSTEM_PROMPT},
                        {"role": "user", "content": t},
                    ],
                    max_tokens=1024,
                    temperature=0.2,
                    timeout=timeout_sec,
                )
                polished = resp.choices[0].message.content.strip()
                # Strip leading/trailing quotation marks if present
                if (polished.startswith('"') and polished.endswith('"')) or (
                    polished.startswith("“") and polished.endswith("”")
                ):
                    polished = polished[1:-1].strip()
                if polished:
                    print(
                        f"[*] AI 润色成功 ({tag}, 耗时 {time.time()-t0:.2f}s), 字数: {len(polished)}",
                        flush=True,
                    )
                    return polished
            except Exception as e:
                print(f"[!] AI 润色 ({tag}) 异常: {type(e).__name__}，尝试备用通道...", flush=True)

        print("[*] AI 润色超时或全部通道失败，安全回退原字上屏", flush=True)
        return t

    def chat(self, user_text: str) -> Tuple[str, List[str]]:
        """
        Chat with Jarvis. Returns (clean_reply, extracted_actions).
        """
        t0 = time.time()
        deadline = time.monotonic() + float(self.config.get("total_timeout", 16))
        self.last_chat_failed = False
        raw_reply = ""
        for client, model_name, tag, timeout_sec in self._channels():
            timeout_sec = min(timeout_sec, deadline - time.monotonic())
            if timeout_sec <= 0:
                break
            if not model_name:
                continue
            try:
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": CHAT_SYSTEM_PROMPT},
                        {"role": "user", "content": user_text or "你好"},
                    ],
                    max_tokens=60,
                    timeout=timeout_sec,
                )
                raw_reply = (resp.choices[0].message.content or "").strip()
                if raw_reply:
                    break
            except Exception as e:
                print(f"[!] LLM 对话 ({tag}) 异常: {type(e).__name__}，尝试备用通道...", flush=True)

        if not raw_reply:
            self.last_chat_failed = True
            print("[!] LLM 对话全部通道失败", flush=True)
            return "连接神经中枢出现波动，请稍后再试。", []

        # Parse [ACTION:xxx] tags
        actions = re.findall(r"\[ACTION:(.*?)\]", raw_reply)
        clean_reply = re.sub(r"\[ACTION:.*?\]", "", raw_reply).strip()

        print(
            f"[*] LLM 对话耗时: {time.time()-t0:.2f}s, 字数: {len(clean_reply)}, 动作数: {len(actions)}",
            flush=True,
        )
        return clean_reply, actions
