"""Синтез речи: протокол, движки, заглушка.

Движок и голос выбираются на каждый язык отдельно (``tts.voices``), потому что
сильные стороны у них разные: Kokoro даёт британские голоса, но не знает
русского; Silero живее на русском, но тянет torch; Piper легче всех.
"""

from __future__ import annotations

import logging

from jarvis.core.audio import AudioSink
from jarvis.core.config import TTSConfig
from jarvis.core.runtime import BlockingWorker

from .backends import (
    BACKENDS,
    EdgeBackend,
    KokoroBackend,
    PiperBackend,
    SileroBackend,
    XttsBackend,
    parse_voice,
)
from .composite import CompositeTTS
from .normalize import normalize_for_speech
from .null import NullTTS
from .protocol import TTS, Speech

logger = logging.getLogger(__name__)

#: Какой пакет нужен каждому движку.
_REQUIREMENTS = {
    "piper": ("piper", "tts"),
    "kokoro": ("kokoro_onnx", "kokoro"),
    "silero": ("torch", "silero"),
    "xtts": ("TTS", "xtts"),
    "edge": ("edge_tts", "edge"),
}


def _engine_available(engine: str) -> bool:
    """Установлен ли пакет, нужный движку."""
    module, extra = _REQUIREMENTS.get(engine, ("piper", "tts"))
    try:
        __import__(module)
    except ImportError as exc:
        logger.warning(
            "Движок %s недоступен (%s). Установи: pip install 'jarvis-core[%s]'",
            engine,
            exc,
            extra,
        )
        return False
    return True


def build_tts(config: TTSConfig, worker: BlockingWorker, *, sink: AudioSink) -> TTS:
    """Создать синтезатор по конфигу с откатом на заглушку."""
    if config.engine in (None, "", "null") and not config.voices:
        return NullTTS(sample_rate=config.sample_rate)

    if not config.voices:
        logger.warning("В tts.voices не задан ни один голос — синтез отключён")
        return NullTTS(sample_rate=config.sample_rate)

    # Движок языка по умолчанию обязан быть рабочим: без него говорить нечем.
    _, spec = config.voice_for(config.default_language)
    engine, _voice = parse_voice(spec, default_engine=config.engine)
    if not _engine_available(engine):
        return NullTTS(sample_rate=config.sample_rate)

    return CompositeTTS(config, worker, sink=sink)


__all__ = [
    "BACKENDS",
    "TTS",
    "CompositeTTS",
    "EdgeBackend",
    "KokoroBackend",
    "NullTTS",
    "PiperBackend",
    "SileroBackend",
    "Speech",
    "XttsBackend",
    "build_tts",
    "normalize_for_speech",
    "parse_voice",
]
