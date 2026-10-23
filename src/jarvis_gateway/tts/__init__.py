"""TTS subpackage."""

from jarvis_gateway.tts.base import BaseTTS
from jarvis_gateway.tts.mac_say import MacSayTTS

__all__ = ["BaseTTS", "MacSayTTS"]
