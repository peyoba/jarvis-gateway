"""
Abstract base class for ASR engines.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseASR(ABC):
    """Base interface for all speech-to-text recognition providers."""

    @abstractmethod
    def transcribe(self, wav_path: str) -> str | None:
        """
        Transcribe audio from a local WAV file.
        Returns transcribed text or None if failed.
        """
        raise NotImplementedError
