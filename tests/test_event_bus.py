"""Шина событий: доставка, шаблоны, изоляция ошибок, отписка."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import pytest

from jarvis.core.bus import LocalEventBus
from jarvis.core.contracts import Event


@dataclass(frozen=True, slots=True, kw_only=True)
class Ping(Event):
    """Тестовое событие."""

    NAME: ClassVar[str] = "test.ping"

    payload: str = ""


async def test_publish_reaches_subscriber(events: LocalEventBus) -> None:
    """Подписчик получает событие с полезной нагрузкой."""
    received: list[str] = []

    async def handler(event: Event) -> None:
        received.append(event.payload)

    events.subscribe("test.ping", handler)
    await events.publish(Ping(payload="привет"))

    assert received == ["привет"]


async def test_wildcard_patterns(events: LocalEventBus) -> None:
    """Префиксная подписка и «*» ловят событие наравне с точным именем."""
    hits: list[str] = []

    async def handler(event: Event) -> None:
        hits.append(event.name)

    events.subscribe("test.*", handler)
    events.subscribe("*", handler)
    events.subscribe("test.ping", handler)

    await events.publish(Ping())

    assert len(hits) == 3


async def test_other_events_not_delivered(events: LocalEventBus) -> None:
    """Подписка на чужое имя не срабатывает."""
    hits: list[str] = []

    async def handler(event: Event) -> None:
        hits.append(event.name)

    events.subscribe("sensor.temperature.changed", handler)
    await events.publish(Ping())

    assert hits == []


async def test_failing_handler_does_not_break_others(events: LocalEventBus) -> None:
    """Падение одного подписчика не мешает остальным — ядро остаётся живым."""
    survived: list[str] = []

    async def broken(event: Event) -> None:
        raise RuntimeError("скилл сломался")

    async def healthy(event: Event) -> None:
        survived.append(event.name)

    events.subscribe("test.ping", broken)
    events.subscribe("test.ping", healthy)

    await events.publish(Ping())  # не должно бросить наружу

    assert survived == ["test.ping"]


async def test_unsubscribe_is_idempotent(events: LocalEventBus) -> None:
    """Отписка прекращает доставку, повторный вызов безопасен."""
    hits: list[str] = []

    async def handler(event: Event) -> None:
        hits.append(event.name)

    subscription = events.subscribe("test.ping", handler)
    await events.publish(Ping())
    subscription.unsubscribe()
    subscription.unsubscribe()
    await events.publish(Ping())

    assert len(hits) == 1


async def test_emit_runs_in_background(events: LocalEventBus) -> None:
    """`emit` не ждёт обработчиков, но событие всё равно доставляется."""
    hits: list[str] = []

    async def handler(event: Event) -> None:
        hits.append(event.name)

    events.subscribe("test.ping", handler)
    events.emit(Ping())

    assert hits == []  # ещё не выполнено
    await events.stop()  # дожидается фоновых публикаций
    assert hits == ["test.ping"]
