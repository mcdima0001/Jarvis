"""Аудиотракт: протоколы и сборка по конфигу."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from jarvis.core.config import AudioConfig

from .null import AlwaysActiveWakeWord, NullAudioSink, NullAudioSource, PassthroughVAD
from .protocol import VAD, AudioFrame, AudioSink, AudioSource, WakeWord

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioStack:
    """Собранный аудиотракт."""

    source: AudioSource
    sink: AudioSink
    vad: VAD
    wake_word: WakeWord


def build_audio(config: AudioConfig) -> AudioStack:
    """Собрать аудиотракт по конфигу.

    Реальные устройства подключаются позже отдельными классами; здесь важно,
    что у каждого звена есть своё место и его можно заменить поштучно.
    """
    if config.vad.engine not in (None, "", "null"):
        logger.warning("Движок VAD %r пока не реализован — пропускаю весь звук", config.vad.engine)
    if config.wake_word.engine not in (None, "", "null"):
        logger.warning(
            "Движок активационной фразы %r пока не реализован — слушаю постоянно",
            config.wake_word.engine,
        )

    return AudioStack(
        source=NullAudioSource(),
        sink=NullAudioSink(),
        vad=PassthroughVAD(),
        wake_word=AlwaysActiveWakeWord(config.wake_word.phrase),
    )


__all__ = [
    "VAD",
    "AlwaysActiveWakeWord",
    "AudioFrame",
    "AudioSink",
    "AudioSource",
    "AudioStack",
    "NullAudioSink",
    "NullAudioSource",
    "PassthroughVAD",
    "WakeWord",
    "build_audio",
]
