"""Unit tests for audio processing: AGC and WAV conversion."""

import array
import os
import wave

import pytest

from jarvis_gateway.audio.agc import apply_adaptive_gain
from jarvis_gateway.audio.converter import (
    AudioLimitError,
    pcm_to_wav_bytes,
    temporary_wav_file,
    wav_bytes_to_pcm16,
)


def test_apply_adaptive_gain():
    # Construct quiet sine/linear samples (peak = 5000)
    samples = array.array("h", [1000, -2000, 3000, -4000, 5000])
    raw_pcm = samples.tobytes()

    # Apply AGC: target is 22000, peak was 5000 -> factor ~ 2.8 clamped
    boosted_pcm = apply_adaptive_gain(raw_pcm)
    assert len(boosted_pcm) == len(raw_pcm)

    boosted_samples = array.array("h")
    boosted_samples.frombytes(boosted_pcm)
    assert max(abs(x) for x in boosted_samples) > 5000


def test_pcm_to_wav_bytes():
    raw_pcm = bytes(3200)  # 100ms silence
    wav_bytes = pcm_to_wav_bytes(raw_pcm)
    assert wav_bytes.startswith(b"RIFF")
    assert b"WAVE" in wav_bytes


def test_temporary_wav_file():
    raw_pcm = bytes(1600)
    with temporary_wav_file(raw_pcm) as wav_path:
        assert os.path.exists(wav_path)
        assert wav_path.endswith(".wav")
    assert not os.path.exists(wav_path)


def test_wav_low_rate_amplification_is_rejected():
    wav_bytes = pcm_to_wav_bytes(bytes(128), sample_rate=1)
    with pytest.raises(AudioLimitError):
        wav_bytes_to_pcm16(wav_bytes)


def test_temporary_wav_file_treats_riff_prefixed_samples_as_pcm():
    # 解码后的样本也可能恰好以 RIFF 开头，不能再次猜测为容器。
    samples = pcm_to_wav_bytes(bytes(128), sample_rate=1)
    with temporary_wav_file(samples, apply_gain=False) as wav_path:
        with wave.open(wav_path, "rb") as wf:
            assert wf.getparams()[:4] == (1, 2, 16000, len(samples) // 2)
            assert wf.readframes(wf.getnframes()) == samples
