"""Роутер — цепочка резолверов.

Роутер не решает, «какой скилл обработает запрос»: он превращает фразу в
`Intent` — имя инструмента и аргументы. Кто владеет инструментом, знает только
реестр. Благодаря этому правила и function-calling LLM взаимозаменяемы, а
порядок и пороги настраиваются из конфига.

Первый резолвер, чья уверенность не ниже порога, побеждает.
"""

from __future__ import annotations

import logging
from typing import Sequence

from jarvis.core.bus import EventBus
from jarvis.core.contracts import Intent, IntentResolved, IntentUnresolved, Utterance

from .protocol import Resolver

logger = logging.getLogger(__name__)


class Router:
    """Последовательный разбор реплики цепочкой резолверов."""

    def __init__(
        self,
        resolvers: Sequence[Resolver],
        *,
        threshold: float = 0.6,
        events: EventBus | None = None,
    ) -> None:
        self._resolvers = list(resolvers)
        self._threshold = threshold
        self._events = events

    @property
    def resolvers(self) -> tuple[str, ...]:
        """Имена резолверов в порядке обхода."""
        return tuple(resolver.name for resolver in self._resolvers)

    async def route(self, utterance: Utterance) -> Intent | None:
        """Разобрать реплику. Возвращает `None`, если никто не справился."""
        last_index = len(self._resolvers) - 1

        for index, resolver in enumerate(self._resolvers):
            try:
                intent = await resolver.resolve(utterance)
            except Exception:
                logger.exception("Резолвер %s упал, иду дальше по цепочке", resolver.name)
                continue

            if intent is None:
                continue

            # Порог защищает от того, чтобы слабая догадка обошла более сильный
            # резолвер дальше по цепочке. За последним звеном никого нет —
            # применять порог там значит просто терять запрос.
            if index < last_index and intent.confidence < self._threshold:
                logger.debug(
                    "Резолвер %s дал %.2f при пороге %.2f — пропускаю",
                    resolver.name,
                    intent.confidence,
                    self._threshold,
                )
                continue

            resolved = intent.with_resolver(resolver.name)
            logger.info(
                "Реплика %r -> %s (резолвер %s, уверенность %.2f)",
                utterance.text,
                resolved.tool,
                resolver.name,
                resolved.confidence,
            )
            if self._events is not None:
                self._events.emit(
                    IntentResolved(
                        source="router",
                        tool=resolved.tool,
                        resolver=resolver.name,
                        confidence=resolved.confidence,
                        utterance=utterance.text,
                    )
                )
            return resolved

        logger.info("Реплика %r не разобрана", utterance.text)
        if self._events is not None:
            self._events.emit(
                IntentUnresolved(
                    source="router",
                    utterance=utterance.text,
                    reason="ни один резолвер не дал результата выше порога",
                )
            )
        return None
