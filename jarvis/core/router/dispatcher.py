"""Диспетчер: реплика -> намерение -> вызов инструмента -> ответ.

Разделение намеренное: роутер занимается пониманием (NLU), диспетчер —
исполнением. Заменить любую из половин можно, не трогая вторую.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from jarvis.core.bus import EventBus
from jarvis.core.contracts import AssistantReplied, ToolResult, Utterance
from jarvis.core.errors import ToolNotFound
from jarvis.core.tools import ToolRegistry

from .resolvers import LearnedResolver
from .router import Router

if TYPE_CHECKING:  # только для типов — зависимости не создаём
    from jarvis.core.situation import Situation

logger = logging.getLogger(__name__)

_NOT_UNDERSTOOD = {
    "ru": "Не понял команду. Повтори, пожалуйста, другими словами.",
    "en": "Sorry, I didn't catch that. Could you rephrase?",
}


class Dispatcher:
    """Проводит реплику через роутер и реестр инструментов."""

    def __init__(
        self,
        *,
        router: Router,
        registry: ToolRegistry,
        events: EventBus | None = None,
        learner: LearnedResolver | None = None,
        situation: "Situation | None" = None,
    ) -> None:
        self._router = router
        self._registry = registry
        self._events = events
        #: Кому отдавать удачные разборы моделью на запоминание.
        self._learner = learner
        #: Куда записывать «что просили в прошлый раз». Диспетчер тут
        #: единственный уместный: он один знает и намерение, и чем всё кончилось.
        self._situation = situation

    def _remember(self, utterance: Utterance, tool: str, ok: bool) -> None:
        """Отметить команду в обстановке для следующего разбора."""
        if self._situation is not None:
            self._situation.command(utterance.text, tool=tool, ok=ok)

    async def handle(self, utterance: Utterance) -> ToolResult:
        """Обработать реплику целиком и вернуть результат."""
        intent = await self._router.route(utterance)
        if intent is None:
            self._remember(utterance, "", False)
            return ToolResult.failure(
                "Намерение не распознано",
                tool="",
                speech=_NOT_UNDERSTOOD,
            )

        try:
            result = await self._registry.invoke(intent.tool, intent.arguments)
        except ToolNotFound as exc:
            logger.error("Роутер выбрал несуществующий инструмент: %s", exc)
            self._remember(utterance, intent.tool, False)
            return ToolResult.failure(str(exc), tool=intent.tool, speech=_NOT_UNDERSTOOD)

        self._remember(utterance, intent.tool, result.ok)

        # Модель разобрала фразу, инструмент отработал — связка проверена
        # делом, и со второго раза она обойдётся без модели. Записывается
        # только успех: закрепить промах хуже, чем не выучить ничего.
        if self._learner is not None and result.ok and intent.resolver == "llm":
            await self._learner.remember(utterance.text, intent)

        spoken = result.speech_for(utterance.language)
        if self._events is not None and spoken:
            self._events.emit(
                AssistantReplied(source="dispatcher", text=spoken, spoken=False)
            )
        return result

    async def handle_text(self, text: str, *, source: str = "text") -> ToolResult:
        """Удобная обёртка для текстовой команды (режим ``--say``, Telegram)."""
        return await self.handle(Utterance(text=text, source=source))
