"""Заглушка синтеза: ответы уходят в лог вместо динамика.

Удобна на сервере без звука и в тестах: конвейер работает целиком, слышно
только через логи.
"""

from __future__ import annotations

import logging

from .protocol import Speech

logger = logging.getLogger(__name__)


class NullTTS:
    """Ничего не синтезирует, но печатает реплики."""

    def __init__(self, *, sample_rate: int = 22050) -> None:
        self._sample_rate = sample_rate

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "tts(null)"

    @property
    def ready(self) -> bool:
        """Заглушка всегда готова."""
        return True

    async def start(self) -> None:
        """Предупредить, что голосовой вывод недоступен."""
        logger.warning("Синтез речи отключён — ответы будут только в логе")

    async def stop(self) -> None:
        """Останавливать нечего."""

    async def synthesize(self, text: str, *, language: str | None = None) -> Speech:
        """Вернуть пустое аудио."""
        return Speech(
            audio=b"",
            sample_rate=self._sample_rate,
            text=text,
            language=language or "",
        )

    async def say(self, text: str, *, language: str | None = None) -> None:
        """Записать реплику в лог."""
        logger.info("[TTS%s] %s", f" {language}" if language else "", text)
