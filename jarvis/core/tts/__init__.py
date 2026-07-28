"""Синтез речи: протокол, Piper, заглушка."""

from __future__ import annotations

import logging

from jarvis.core.audio import AudioSink
from jarvis.core.config import TTSConfig
from jarvis.core.runtime import BlockingWorker

from .null import NullTTS
from .piper import PiperTTS
from .protocol import TTS, Speech

logger = logging.getLogger(__name__)


def build_tts(config: TTSConfig, worker: BlockingWorker, *, sink: AudioSink) -> TTS:
    """Создать синтезатор по конфигу с откатом на заглушку."""
    if config.engine in (None, "", "null"):
        return NullTTS(sample_rate=config.sample_rate)

    if config.engine == "piper":
        try:
            import piper  # noqa: F401
        except ImportError as exc:
            logger.warning(
                "Piper недоступен (%s) — синтез отключён. "
                "Установи: pip install 'jarvis-core[tts]'",
                exc,
            )
            return NullTTS(sample_rate=config.sample_rate)

        _, voice = config.voice_for(config.default_language)
        if not voice:
            logger.warning("В tts.voices не задан ни один голос — синтез отключён")
            return NullTTS(sample_rate=config.sample_rate)

        model_path = config.models_dir / f"{voice}.onnx"
        if not model_path.is_file():
            logger.warning(
                "Голос Piper не найден: %s — синтез отключён. "
                "Скачай: python -m jarvis --download-voice %s",
                model_path,
                voice,
            )
            return NullTTS(sample_rate=config.sample_rate)

        missing = [
            name
            for code, name in config.voices.items()
            if not (config.models_dir / f"{name}.onnx").is_file()
        ]
        if missing:
            logger.warning(
                "Не скачаны голоса: %s — на этих языках буду отвечать голосом «%s»",
                ", ".join(missing),
                voice,
            )

        return PiperTTS(config, worker, sink=sink)

    logger.warning("Неизвестный движок TTS %r — использую заглушку", config.engine)
    return NullTTS(sample_rate=config.sample_rate)


__all__ = ["TTS", "NullTTS", "PiperTTS", "Speech", "build_tts"]
