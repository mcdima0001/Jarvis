"""Контракты памяти.

Разделы памяти разной природы, поэтому и интерфейса два:

* `DocumentStore` — изменяемые документы (profile, preferences, studio).
  Доступ по ключу, типичная операция «прочитал, поправил, записал».
* `JournalStore` — журналы (today, history). Только добавление и выборка
  по времени или тегу.

Один общий интерфейс на оба дал бы либо неэффективный журнал, либо неудобный
документ, поэтому они разведены.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True, slots=True, kw_only=True)
class JournalEntry:
    """Запись журнала."""

    timestamp: float
    text: str
    tags: tuple[str, ...] = ()
    data: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class DocumentStore(Protocol):
    """Изменяемые документы, разбитые по разделам."""

    def namespaces(self) -> tuple[str, ...]:
        """Доступные разделы."""
        ...

    async def read(self, namespace: str) -> dict[str, Any]:
        """Прочитать раздел целиком."""
        ...

    async def write(self, namespace: str, data: Mapping[str, Any]) -> None:
        """Полностью заменить содержимое раздела."""
        ...

    async def update(self, namespace: str, values: Mapping[str, Any]) -> dict[str, Any]:
        """Обновить часть ключей раздела."""
        ...

    async def get(self, namespace: str, key: str, default: Any = None) -> Any:
        """Прочитать одно значение."""
        ...

    async def set(self, namespace: str, key: str, value: Any) -> None:
        """Записать одно значение."""
        ...


@runtime_checkable
class JournalStore(Protocol):
    """Журналы: только добавление и выборка."""

    def namespaces(self) -> tuple[str, ...]:
        """Доступные журналы."""
        ...

    async def append(
        self,
        namespace: str,
        text: str,
        *,
        tags: Sequence[str] = (),
        data: Mapping[str, Any] | None = None,
    ) -> JournalEntry:
        """Добавить запись."""
        ...

    async def recent(
        self,
        namespace: str,
        *,
        limit: int = 20,
        since: float | None = None,
        tag: str | None = None,
    ) -> list[JournalEntry]:
        """Вернуть последние записи с фильтрами."""
        ...
