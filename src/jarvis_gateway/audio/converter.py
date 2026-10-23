"""
Audio utilities: PCM to WAV conversion and temporary file helpers.
"""

from __future__ import annotations

import array
import io
import os
import tempfile
import wave
from contextlib import contextmanager
from typing import Generator

from jarvis_gateway.audio.agc import apply_adaptive_gain


def pcm_to_wav_bytes(
    pcm_data: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """Pack raw PCM samples into standard RIFF WAV bytes in-memory."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()


def wav_bytes_to_pcm16(data: bytes, sample_rate: int = 16000) -> bytes:
    """
    Decode a RIFF/WAVE byte string into mono 16-bit PCM at sample_rate.

    Never assume a 44-byte header: `say` and other encoders emit LIST/fact chunks,
    so the data chunk can start at byte 4096 or later. Returns b"" on bad input.
    """
    if not data:
        return b""
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
    except Exception:
        return b""
    if not raw or width != 2:
        return b""

    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    if channels > 1:
        samples = array.array("h", samples[::channels])
    if rate != sample_rate and rate > 0 and len(samples) > 1:
        ratio = sample_rate / float(rate)
        n_out = max(1, int(len(samples) * ratio))
        last = len(samples) - 1
        samples = array.array("h", (samples[min(last, int(i / ratio))] for i in range(n_out)))
    return samples.tobytes()


@contextmanager
def temporary_wav_file(
    audio_data: bytes,
    apply_gain: bool = True,
    sample_rate: int = 16000,
) -> Generator[str, None, None]:
    """
    Context manager that ensures audio_data is written to a temporary .wav file.
    Automatically applies AGC if raw PCM without RIFF header.
    Guarantees clean removal on exit.
    """
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        wav_path = tf.name

    try:
        if audio_data.startswith(b"RIFF"):
            with open(wav_path, "wb") as f:
                f.write(audio_data)
        else:
            processed = apply_adaptive_gain(audio_data) if apply_gain else audio_data
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(processed)
        yield wav_path
    finally:
        if os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except OSError:
                pass
