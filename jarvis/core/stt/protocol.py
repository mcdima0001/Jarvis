"""Контракт распознавания речи."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True, kw_only=True)
class Transcript:
    """Результат распознавания."""

    text: str
    language: str = ""
    confidence: float = 1.0
    duration: float = 0.0

    @property
    def empty(self) -> bool:
        """Пустой ли результат."""
        return not self.text.strip()


@runtime_checkable
class STT(Protocol):
    """Распознаватель речи."""

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        ...

    @property
    def ready(self) -> bool:
        """Загружена ли модель."""
        ...

    async def start(self) -> None:
        """Загрузить модель."""
        ...

    async def stop(self) -> None:
        """Выгрузить модель."""
        ...

    async def transcribe(self, audio: bytes, *, sample_rate: int = 16000) -> Transcript:
        """Распознать моно-PCM 16 бит."""
        ...
