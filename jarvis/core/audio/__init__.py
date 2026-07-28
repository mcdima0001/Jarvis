"""Аудиотракт: протоколы, реализации и сборка по конфигу."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from jarvis.core.config import AudioConfig
from jarvis.core.errors import AudioError

from .devices import SoundDeviceSink, SoundDeviceSource, list_devices
from .null import AlwaysActiveWakeWord, NullAudioSink, NullAudioSource, PassthroughVAD
from .sound import load_sound, trim_silence
from .protocol import VAD, AudioFrame, AudioSink, AudioSource, WakeWord
from .vad import EnergyVAD, frame_rms

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioStack:
    """Собранный аудиотракт."""

    source: AudioSource
    sink: AudioSink
    vad: VAD
    wake_word: WakeWord

    @property
    def live(self) -> bool:
        """Работает ли реальный звук (а не заглушки)."""
        return not isinstance(self.source, NullAudioSource)


def _build_vad(config: AudioConfig) -> VAD:
    """Выбрать детектор речи по конфигу."""
    if config.vad.engine in ("", "none", "null"):
        return PassthroughVAD()
    if config.vad.engine == "energy":
        return EnergyVAD(
            threshold=config.vad.threshold,
            calibrate_frames=max(1, int(config.vad.calibrate_seconds * 1000 / config.frame_ms)),
        )
    logger.warning(
        "Неизвестный движок VAD %r — пропускаю весь звук. Доступен: energy",
        config.vad.engine,
    )
    return PassthroughVAD()


def build_audio(config: AudioConfig) -> AudioStack:
    """Собрать аудиотракт по конфигу.

    Если звуковая подсистема недоступна (нет устройства, не установлен
    sounddevice), поднимаются заглушки: приложение должно стартовать и на
    сервере без звуковой карты.
    """
    vad = _build_vad(config)
    # Активация по имени проверяется по распознанному тексту в конвейере;
    # этот слот оставлен под будущий детектор по звуку (openWakeWord).
    wake_word = AlwaysActiveWakeWord(config.wake_word.phrase)

    if config.engine in ("", "none", "null"):
        logger.info("Звук отключён в конфиге (audio.engine)")
        return AudioStack(
            source=NullAudioSource(),
            sink=NullAudioSink(),
            vad=vad,
            wake_word=wake_word,
        )

    if config.engine != "sounddevice":
        logger.warning(
            "Неизвестный движок звука %r — использую заглушки. Доступен: sounddevice",
            config.engine,
        )
        return AudioStack(
            source=NullAudioSource(),
            sink=NullAudioSink(),
            vad=vad,
            wake_word=wake_word,
        )

    try:
        from .devices import _import_sounddevice

        _import_sounddevice()
    except AudioError as exc:
        logger.warning("%s Звук отключён, команды доступны через --say.", exc)
        return AudioStack(
            source=NullAudioSource(),
            sink=NullAudioSink(),
            vad=vad,
            wake_word=wake_word,
        )

    return AudioStack(
        source=SoundDeviceSource(config),
        sink=SoundDeviceSink(config),
        vad=vad,
        wake_word=wake_word,
    )


__all__ = [
    "load_sound",
    "trim_silence",
    "VAD",
    "AlwaysActiveWakeWord",
    "AudioFrame",
    "AudioSink",
    "AudioSource",
    "AudioStack",
    "EnergyVAD",
    "NullAudioSink",
    "NullAudioSource",
    "PassthroughVAD",
    "SoundDeviceSink",
    "SoundDeviceSource",
    "WakeWord",
    "build_audio",
    "frame_rms",
    "list_devices",
]
