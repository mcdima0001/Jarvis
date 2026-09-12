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
    language: str = ""

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

    async def synthesize(self, text: str, *, language: str | None = None) -> Speech:
        """Синтезировать речь голосом нужного языка."""
        ...

    async def prewarm(self, text: str, *, language: str | None = None) -> None:
        """Приготовить реплику заранее, ничего не произнося.

        Синтез свежего текста стоит около полутора секунд, готовая реплика —
        миллисекунды. Кто знает, что скажет, но не знает когда, платит это время
        заранее.
        """
        ...

    async def say(self, text: str, *, language: str | None = None) -> None:
        """Синтезировать и произнести."""
        ...
