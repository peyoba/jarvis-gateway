"""
ASR Orchestrator: Multi-provider dispatch, hallucination filtering, and latency tracking.
"""

from __future__ import annotations

import re
import time

from jarvis_gateway.asr.whisper_cli import WhisperCliASR
from jarvis_gateway.asr.whisper_server import WhisperServerASR

# Whisper silence/repetition artefacts. Matched as whole phrases, not substrings:
# a user saying "再见" or "订阅" must still be typed.
_HALLUCINATION_PHRASES = {
    "字幕",
    "字幕由amaraorg社区提供",
    "谢谢大家",
    "谢谢收看",
    "谢谢观看",
    "感谢观看",
    "请不吝点赞订阅",
    "请不吝点赞订阅转发打赏支持明镜与点点栏目",
    "thanksforwatching",
    "thankyouforwatching",
    "pleasesubscribe",
}
_HALLUCINATION_PATTERNS = (
    re.compile(r"^字幕(?:由|提供|[:：])"),
    re.compile(r"^(?:请)?不吝?点赞.*(?:订阅|转发|打赏)"),
    re.compile(r"^(?:谢谢|感谢)(?:大家|收看|观看|收听)"),
    re.compile(r"^(?:amaraorg|明镜与点点)"),
)


def _normalize(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?~～…\-]+", "", text).lower()


def is_hallucination(text: str | None, audio_sec: float | None = None) -> bool:
    """Detect Whisper silence/repetition hallucinations without dropping real speech."""
    if not text:
        return True
    t = _normalize(text)
    if len(t) <= 1:
        return True
    # Long recordings are real speech even if they end with a stock phrase.
    if audio_sec is not None and audio_sec > 5.0:
        return False
    if t in _HALLUCINATION_PHRASES:
        return True
    if any(p.search(t) for p in _HALLUCINATION_PATTERNS):
        return True
    # Degenerate repetition, e.g. "啊啊啊啊啊啊" or "。。。"
    return len(t) >= 6 and len(set(t)) <= 2


class ASRManager:
    def __init__(
        self,
        server_asr: WhisperServerASR,
        cli_fallback_asr: WhisperCliASR | None = None,
    ):
        self.server_asr = server_asr
        self.cli_fallback_asr = cli_fallback_asr

    def transcribe(self, wav_path: str) -> tuple[str, float, str]:
        """
        Transcribe audio file.
        Returns (recognized_text, latency_seconds, provider_tag).
        """
        t0 = time.time()

        # 1. Try resident whisper-server
        text = self.server_asr.transcribe(wav_path)
        if text is not None:
            elapsed = time.time() - t0
            print(f"[*] ASR (server) 耗时: {elapsed:.2f}s, 听到: '{text}'", flush=True)
            return text, elapsed, "whisper-server"

        # 2. Try CLI fallback
        if self.cli_fallback_asr and self.cli_fallback_asr.is_available():
            print("[!] whisper-server 未响应，回退 whisper-cli...", flush=True)
            text = self.cli_fallback_asr.transcribe(wav_path)
            if text is not None:
                elapsed = time.time() - t0
                print(f"[*] ASR (cli) 耗时: {elapsed:.2f}s, 听到: '{text}'", flush=True)
                return text, elapsed, "whisper-cli"

        elapsed = time.time() - t0
        print(f"[!] ASR 全部通道失败或超时 ({elapsed:.2f}s)", flush=True)
        return "", elapsed, "none"
