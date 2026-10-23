"""
Abstract base class for Text-To-Speech (TTS) engines.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseTTS(ABC):
    """Base interface for all TTS speech synthesizers."""

    @abstractmethod
    def synthesize(self, text: str) -> bytes:
        """
        Synthesize text into raw 16kHz 16-bit Mono PCM bytes.
        Returns empty bytes on failure.
        """
        raise NotImplementedError
