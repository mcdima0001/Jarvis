"""Заглушка распознавания: система стартует без модели Whisper."""

from __future__ import annotations

import logging

from .protocol import Transcript

logger = logging.getLogger(__name__)


class NullSTT:
    """Ничего не распознаёт, но не мешает приложению работать."""

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "stt(null)"

    @property
    def ready(self) -> bool:
        """Заглушка всегда «готова», но ничего не умеет."""
        return True

    async def start(self) -> None:
        """Предупредить, что голосовой ввод недоступен."""
        logger.warning("Распознавание речи отключено — голосовой ввод недоступен")

    async def stop(self) -> None:
        """Останавливать нечего."""

    async def transcribe(self, audio: bytes, *, sample_rate: int = 16000) -> Transcript:
        """Вернуть пустой результат."""
        return Transcript(text="")
