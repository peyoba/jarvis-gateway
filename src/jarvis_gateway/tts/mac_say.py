"""
macOS Native TTS Synthesizer using the built-in `say` command.
Generates 16kHz 16-bit Mono PCM audio.
"""

from __future__ import annotations

import array
import os
import subprocess
import tempfile
import time

from jarvis_gateway.audio.converter import wav_bytes_to_pcm16
from jarvis_gateway.tts.base import BaseTTS


class MacSayTTS(BaseTTS):
    def __init__(self, voice_name: str = "Tingting", gain: float = 1.3):
        self.voice_name = voice_name
        self.gain = gain

    def synthesize(self, text: str) -> bytes:
        if not text:
            return b""

        t0 = time.time()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            out_wav = tf.name

        try:
            # say -v Tingting "text" -o out.wav --data-format=LEI16@16000
            subprocess.run(
                ["say", "-v", self.voice_name, text, "-o", out_wav, "--data-format=LEI16@16000"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15.0,
            )
            with open(out_wav, "rb") as f:
                wav_data = f.read()

            # Parse the RIFF chunks instead of assuming a 44-byte header.
            pcm_raw = wav_bytes_to_pcm16(wav_data)
            if not pcm_raw:
                return b""

            # Apply output volume gain
            a = array.array("h")
            a.frombytes(pcm_raw)
            gain = self.gain
            for i in range(len(a)):
                val = int(a[i] * gain)
                a[i] = max(-32000, min(32000, val))
            pcm_data = a.tobytes()

            print(f"[*] TTS 耗时: {time.time()-t0:.2f}s, 生成 PCM: {len(pcm_data)} 字节", flush=True)
            return pcm_data
        except Exception as e:
            print(f"[!] TTS 生成异常: {e}", flush=True)
            return b""
        finally:
            if os.path.exists(out_wav):
                try:
                    os.remove(out_wav)
                except OSError:
                    pass
