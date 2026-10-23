"""
Whisper-Server HTTP ASR Client.
Calls resident whisper.cpp server running on localhost (Metal GPU accelerated).
"""

from __future__ import annotations

import json
import urllib.request

from jarvis_gateway.asr.base import BaseASR


class WhisperServerASR(BaseASR):
    def __init__(
        self,
        port: int = 18889,
        host: str = "127.0.0.1",
        language: str = "zh",
        timeout: float = 60.0,
        prompt: str = "",
        beam_size: int = 0,
    ):
        self.host = host
        self.port = port
        self.language = language
        self.timeout = timeout
        self.prompt = prompt
        self.beam_size = beam_size
        self.endpoint = f"http://{host}:{port}/inference"

    def transcribe(self, wav_path: str) -> str | None:
        boundary = "----JarvisBoundary7MA4YWxkTrZu0gW"
        try:
            with open(wav_path, "rb") as f:
                audio_bytes = f.read()
        except Exception as e:
            print(f"[ASR Server] 读取音频失败 ({wav_path}): {e}", flush=True)
            return None

        fields = [
            ("language", self.language),
            ("temperature", "0.0"),
            ("response_format", "json"),
            ("no_timestamps", "true"),
        ]
        if self.prompt:
            fields.append(("prompt", self.prompt))
        if self.beam_size and self.beam_size > 1:
            fields.append(("beam_size", str(self.beam_size)))
        chunks = []
        for name, value in fields:
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            )
        chunks.append(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
             f'Content-Type: audio/wav\r\n\r\n').encode()
        )
        chunks.append(audio_bytes)
        chunks.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(chunks)

        req = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return (data.get("text") or "").strip()
        except Exception as e:
            print(f"[ASR Server] 请求失败 ({self.endpoint}): {e}", flush=True)
            return None
