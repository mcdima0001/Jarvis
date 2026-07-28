"""Scope скилла — всё, что он зарегистрировал, и как это отозвать.

Скилл не трогает шину и реестр напрямую: он проходит через scope, а тот
запоминает каждую подписку, каждый инструмент и каждую фоновую задачу.
Выгрузка скилла превращается в один вызов `revoke()` — ничего не остаётся
висеть.

Это же и есть задел под hot reload: перезагрузка = отозвать scope, переимпортировать
модуль, создать новый scope.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine

from jarvis.core.bus import EventBus, EventHandler, Subscription
from jarvis.core.tools import Registration, Tool, ToolRegistry

logger = logging.getLogger(__name__)


class SkillScope:
    """Учёт регистраций одного скилла."""

    def __init__(self, *, skill: str, events: EventBus, tools: ToolRegistry) -> None:
        self._skill = skill
        self._events = events
        self._tools = tools
        self._subscriptions: list[Subscription] = []
        self._registrations: list[Registration] = []
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def skill(self) -> str:
        """Имя скилла, которому принадлежит scope."""
        return self._skill

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Инструменты, зарегистрированные через этот scope."""
        return tuple(registration.name for registration in self._registrations)

    def subscribe(self, pattern: str, handler: EventHandler) -> Subscription:
        """Подписаться на события; подписка снимется вместе со скиллом."""
        subscription = self._events.subscribe(pattern, handler)
        self._subscriptions.append(subscription)
        return subscription

    def register_tool(self, tool: Tool) -> Registration:
        """Зарегистрировать инструмент; он снимется вместе со скиллом."""
        registration = self._tools.register(tool)
        self._registrations.append(registration)
        return registration

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task[Any]:
        """Запустить фоновую задачу скилла; она будет отменена при выгрузке."""
        task = asyncio.create_task(coro, name=name or f"{self._skill}-task")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def revoke(self) -> None:
        """Отозвать всё: задачи, подписки, инструменты."""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()

        for registration in self._registrations:
            registration.revoke()
        self._registrations.clear()

        logger.debug("Scope скилла %s отозван", self._skill)
