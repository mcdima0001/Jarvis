"""Контракт шины событий.

Шина отвечает **только** за факты: «событие произошло». Она ничего не возвращает
и не знает, кто и как отреагирует. За командами с ответом — в `ToolRegistry`.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol, runtime_checkable

from jarvis.core.contracts import Event

#: Обработчик события. Всегда асинхронный и ничего не возвращает.
EventHandler = Callable[[Event], Awaitable[None]]


@runtime_checkable
class Subscription(Protocol):
    """Токен подписки: позволяет отписаться."""

    @property
    def pattern(self) -> str:
        """Шаблон, на который оформлена подписка."""
        ...

    def unsubscribe(self) -> None:
        """Снять подписку. Повторный вызов безопасен."""
        ...


@runtime_checkable
class EventBus(Protocol):
    """Шина событий."""

    def subscribe(self, pattern: str, handler: EventHandler) -> Subscription:
        """Подписаться на события.

        :param pattern: точное имя (``sensor.temperature.changed``),
            префикс с ``*`` (``sensor.*``) или ``*`` для всех событий.
        """
        ...

    async def publish(self, event: Event) -> None:
        """Опубликовать событие и дождаться всех обработчиков."""
        ...

    def emit(self, event: Event) -> None:
        """Опубликовать событие, не дожидаясь обработчиков (fire-and-forget)."""
        ...
