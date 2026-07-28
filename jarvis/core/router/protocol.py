"""Контракт резолвера.

Резолвер — одно звено цепочки маршрутизации. Он получает реплику и либо
возвращает `Intent`, либо `None` (тогда запрос уходит следующему звену).

Такая форма делает детерминированные правила и function-calling LLM
взаимозаменяемыми: оба возвращают одно и то же — имя инструмента и аргументы.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from jarvis.core.contracts import Intent, Utterance


@runtime_checkable
class Resolver(Protocol):
    """Звено цепочки маршрутизации."""

    @property
    def name(self) -> str:
        """Имя резолвера — попадает в `Intent.resolver` и в логи."""
        ...

    async def resolve(self, utterance: Utterance) -> Intent | None:
        """Разобрать реплику или вернуть `None`, если не получилось."""
        ...
