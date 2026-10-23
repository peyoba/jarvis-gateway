"""
Audio processing: Adaptive Gain Control (AGC) for weak microphone speech.
"""

from __future__ import annotations

import array


def apply_adaptive_gain(
    pcm_data: bytes,
    min_threshold: int = 14000,
    target_peak: float = 22000.0,
    max_factor: float = 2.8,
) -> bytes:
    """
    Adaptive Gain Control (AGC):
    Detects recording peak amplitude for 16-bit PCM mono audio.
    If speech is soft (peak < min_threshold), smoothly scales up to target_peak (clamped by max_factor)
    with hard-clipping protection [-32767, 32767].
    """
    if not pcm_data or len(pcm_data) < 2:
        return pcm_data

    try:
        a = array.array("h")
        a.frombytes(pcm_data)
        if not a:
            return pcm_data

        max_val = max(abs(x) for x in a)
        if 0 < max_val < min_threshold:
            factor = min(max_factor, target_peak / max_val)
            for i in range(len(a)):
                val = int(a[i] * factor)
                a[i] = max(-32767, min(32767, val))
            return a.tobytes()
    except Exception:
        # Fall back to original bytes on any parsing error
        pass

    return pcm_data
