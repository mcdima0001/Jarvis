"""Диспетчер: реплика -> намерение -> вызов инструмента -> ответ.

Разделение намеренное: роутер занимается пониманием (NLU), диспетчер —
исполнением. Заменить любую из половин можно, не трогая вторую.
"""

from __future__ import annotations

import logging

from jarvis.core.bus import EventBus
from jarvis.core.contracts import AssistantReplied, ToolResult, Utterance
from jarvis.core.errors import ToolNotFound
from jarvis.core.tools import ToolRegistry

from .router import Router

logger = logging.getLogger(__name__)

_NOT_UNDERSTOOD = "Не понял команду. Повтори, пожалуйста, другими словами."


class Dispatcher:
    """Проводит реплику через роутер и реестр инструментов."""

    def __init__(
        self,
        *,
        router: Router,
        registry: ToolRegistry,
        events: EventBus | None = None,
    ) -> None:
        self._router = router
        self._registry = registry
        self._events = events

    async def handle(self, utterance: Utterance) -> ToolResult:
        """Обработать реплику целиком и вернуть результат."""
        intent = await self._router.route(utterance)
        if intent is None:
            return ToolResult.failure(
                "Намерение не распознано",
                tool="",
                speech=_NOT_UNDERSTOOD,
            )

        try:
            result = await self._registry.invoke(intent.tool, intent.arguments)
        except ToolNotFound as exc:
            logger.error("Роутер выбрал несуществующий инструмент: %s", exc)
            return ToolResult.failure(str(exc), tool=intent.tool, speech=_NOT_UNDERSTOOD)

        if self._events is not None and result.speech:
            self._events.emit(
                AssistantReplied(source="dispatcher", text=result.speech, spoken=False)
            )
        return result

    async def handle_text(self, text: str, *, source: str = "text") -> ToolResult:
        """Удобная обёртка для текстовой команды (режим ``--say``, Telegram)."""
        return await self.handle(Utterance(text=text, source=source))
