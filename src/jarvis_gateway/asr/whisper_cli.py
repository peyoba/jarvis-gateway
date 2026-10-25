"""
Whisper-CLI local subprocess ASR Provider (fallback).
"""

from __future__ import annotations

import os
import subprocess

from jarvis_gateway.asr.base import BaseASR


class WhisperCliASR(BaseASR):
    def __init__(
        self,
        cli_bin: str,
        model_path: str,
        language: str = "zh",
        prompt: str = "",
        beam_size: int = 2,
    ):
        self.cli_bin = os.path.expanduser(cli_bin)
        self.model_path = os.path.expanduser(model_path)
        self.language = language
        self.prompt = prompt
        self.beam_size = str(beam_size)

    def is_available(self) -> bool:
        return os.path.isfile(self.cli_bin) and os.path.isfile(self.model_path)

    def transcribe(self, wav_path: str) -> str | None:
        if not self.is_available():
            return None

        cmd = [
            self.cli_bin,
            "-m", self.model_path,
            "-l", self.language,
            "-bs", self.beam_size,
            "--no-timestamps",
            "-nt",
            "-np",
        ]
        if self.prompt:
            cmd.extend(["--prompt", self.prompt])
        cmd.append(wav_path)

        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if p.returncode:
                return None
            lines = [
                line.strip()
                for line in p.stdout.splitlines()
                if line.strip()
                and not line.startswith("[")
                and not line.startswith("whisper_")
                and not line.startswith("load_")
                and not line.startswith("ggml_")
                and not line.startswith("system_info")
                and not line.startswith("main:")
            ]
            return "".join(lines).strip()
        except Exception as e:
            print(f"[ASR CLI] 转写失败: {type(e).__name__}", flush=True)
            return None
