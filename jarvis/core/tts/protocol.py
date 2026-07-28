"""Контракт синтеза речи."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True, kw_only=True)
class Speech:
    """Синтезированная реплика."""

    audio: bytes
    sample_rate: int
    text: str = ""

    @property
    def empty(self) -> bool:
        """Есть ли что воспроизводить."""
        return not self.audio


@runtime_checkable
class TTS(Protocol):
    """Синтезатор речи."""

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        ...

    @property
    def ready(self) -> bool:
        """Загружен ли голос."""
        ...

    async def start(self) -> None:
        """Загрузить голос."""
        ...

    async def stop(self) -> None:
        """Освободить ресурсы."""
        ...

    async def synthesize(self, text: str) -> Speech:
        """Синтезировать речь из текста."""
        ...

    async def say(self, text: str) -> None:
        """Синтезировать и произнести."""
        ...
