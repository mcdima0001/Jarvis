"""Шина событий внутри процесса, на asyncio.

Обработчики одного события выполняются параллельно, ошибки изолируются:
падение одного подписчика не мешает остальным и не роняет публикацию.
Именно это делает скиллы безопасными для ядра.

Заменить реализацию (Redis, NATS, MQTT) можно, не трогая скиллы: они зависят
от протокола `EventBus` и контрактов событий, а не от этого класса.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from jarvis.core.contracts import Event

from .protocol import EventHandler

logger = logging.getLogger(__name__)

_WILDCARD = "*"


class _Subscription:
    """Токен подписки с идемпотентной отпиской."""

    __slots__ = ("_bus", "_pattern", "_handler", "_active")

    def __init__(self, bus: "LocalEventBus", pattern: str, handler: EventHandler) -> None:
        self._bus = bus
        self._pattern = pattern
        self._handler = handler
        self._active = True

    @property
    def pattern(self) -> str:
        """Шаблон подписки."""
        return self._pattern

    def unsubscribe(self) -> None:
        """Снять подписку; повторные вызовы игнорируются."""
        if self._active:
            self._bus._remove(self._pattern, self._handler)
            self._active = False


class LocalEventBus:
    """Реализация `EventBus` для одного процесса."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._background: set[asyncio.Task[None]] = set()

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "event-bus"

    async def start(self) -> None:
        """Шине нечего поднимать — метод есть ради единого контракта сервиса."""

    async def stop(self) -> None:
        """Дождаться фоновых публикаций и снять все подписки."""
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        self._handlers.clear()

    # --- подписка ----------------------------------------------------------

    def subscribe(self, pattern: str, handler: EventHandler) -> _Subscription:
        """Подписаться на имя, префикс ``область.*`` или ``*``."""
        self._handlers[pattern].append(handler)
        logger.debug("Подписка на %s: %s", pattern, getattr(handler, "__qualname__", handler))
        return _Subscription(self, pattern, handler)

    def _remove(self, pattern: str, handler: EventHandler) -> None:
        """Убрать обработчик из списка (вызывается токеном подписки)."""
        handlers = self._handlers.get(pattern)
        if not handlers:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            return
        if not handlers:
            self._handlers.pop(pattern, None)

    def _match(self, name: str) -> list[EventHandler]:
        """Собрать обработчики, подходящие под имя события."""
        matched: list[EventHandler] = list(self._handlers.get(name, ()))
        matched.extend(self._handlers.get(_WILDCARD, ()))
        # Префиксные подписки: sensor.* ловит sensor.temperature.changed
        parts = name.split(".")
        for depth in range(1, len(parts)):
            matched.extend(self._handlers.get(".".join(parts[:depth]) + ".*", ()))
        return matched

    # --- публикация --------------------------------------------------------

    async def publish(self, event: Event) -> None:
        """Опубликовать событие и дождаться всех обработчиков."""
        handlers = self._match(event.name)
        if not handlers:
            logger.debug("Событие %s без подписчиков", event.name)
            return
        logger.debug("Событие %s -> %d подписчик(ов)", event.name, len(handlers))
        await asyncio.gather(*(self._safe_call(h, event) for h in handlers))

    def emit(self, event: Event) -> None:
        """Опубликовать событие в фоне, не дожидаясь обработчиков."""
        task = asyncio.create_task(self.publish(event))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _safe_call(self, handler: EventHandler, event: Event) -> None:
        """Вызвать обработчик, изолировав его ошибки от остальных подписчиков."""
        try:
            await handler(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Обработчик %s упал на событии %s",
                getattr(handler, "__qualname__", handler),
                event.name,
            )
