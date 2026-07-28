"""Резолвер на языковой модели — последнее звено перед свободным диалогом.

Сюда запрос попадает, только если детерминированные резолверы не справились.
Так выполняется главное требование к маршрутизации: типовые команды студии
никогда не уходят в сеть, а платный вызов происходит лишь тогда, когда без
него действительно не обойтись.

Модель видит каталог инструментов (function-calling) и не знает ничего о
внутреннем устройстве скиллов.
"""

from __future__ import annotations

import logging

from jarvis.core.contracts import Intent, Utterance
from jarvis.core.llm import LLMService
from jarvis.core.tools import ToolRegistry

logger = logging.getLogger(__name__)


class LLMResolver:
    """Разбор намерения через function-calling."""

    def __init__(
        self,
        registry: ToolRegistry,
        llm: LLMService,
        *,
        task: str = "intent",
    ) -> None:
        self._registry = registry
        self._llm = llm
        self._task = task

    @property
    def name(self) -> str:
        """Имя резолвера."""
        return "llm"

    async def resolve(self, utterance: Utterance) -> Intent | None:
        """Спросить модель, какой инструмент подходит."""
        if not self._llm.available:
            logger.debug("LLM не настроена — резолвер пропускает запрос дальше")
            return None

        catalog = self._registry.catalog()
        if not catalog.specs:
            return None

        call = await self._llm.extract_intent(utterance.text, catalog, task=self._task)
        if call is None:
            return None

        tool_name = self._registry.resolve_function_name(call.name)
        if tool_name is None:
            logger.warning("Модель предложила неизвестный инструмент %r", call.name)
            return None

        # Подсказка на будущее: каждая такая фраза стоит денег, потому что
        # тащит в модель весь каталог инструментов. Если формулировка
        # повторяется — ей место в phrases скилла, и тогда она бесплатна.
        logger.info(
            "Фраза %r разобрана моделью в %s. Повторяется — добавь её в phrases скилла, "
            "тогда обращения к модели не будет",
            utterance.text,
            tool_name,
        )

        return Intent(
            tool=tool_name,
            arguments=call.arguments,
            confidence=0.85,
            resolver=self.name,
            utterance=utterance.text,
        )
