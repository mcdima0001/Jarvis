"""Пустой аудиотракт: приложение поднимается без микрофона и колонок.

Это рабочий режим для разработки на сервере и для тестов: конвейер собирается
целиком, команды подаются текстом через ``--say``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from .protocol import AudioFrame

logger = logging.getLogger(__name__)


class NullAudioSource:
    """Источник, который никогда не выдаёт кадров."""

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "audio-in(null)"

    async def start(self) -> None:
        """Предупредить, что микрофона нет."""
        logger.warning("Аудиовход отключён — голосовые команды недоступны")

    async def stop(self) -> None:
        """Закрывать нечего."""

    async def frames(self) -> AsyncIterator[AudioFrame]:
        """Бесконечно ждать, не выдавая кадров."""
        while True:
            await asyncio.sleep(3600)
            yield AudioFrame(data=b"", sample_rate=16000)  # pragma: no cover


class NullAudioSink:
    """Вывод, который молча проглатывает звук."""

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "audio-out(null)"

    async def start(self) -> None:
        """Открывать нечего."""

    async def stop(self) -> None:
        """Закрывать нечего."""

    async def play(self, audio: bytes, *, sample_rate: int) -> None:
        """Записать факт воспроизведения в лог."""
        logger.debug("Пропущено %d байт аудио (вывод отключён)", len(audio))


class PassthroughVAD:
    """VAD, который считает речью любой непустой кадр."""

    def is_speech(self, frame: AudioFrame) -> bool:
        """Любой кадр с данными считается речью."""
        return bool(frame.data)

    def reset(self) -> None:
        """Состояния нет — сбрасывать нечего."""


class AlwaysActiveWakeWord:
    """Детектор без активационной фразы: ассистент слушает постоянно."""

    def __init__(self, phrase: str = "") -> None:
        self._phrase = phrase

    @property
    def phrase(self) -> str:
        """Настроенная фраза (используется только для отображения)."""
        return self._phrase

    def detect(self, frame: AudioFrame) -> bool:
        """Считать, что фраза прозвучала всегда."""
        return True

    def reset(self) -> None:
        """Состояния нет — сбрасывать нечего."""
