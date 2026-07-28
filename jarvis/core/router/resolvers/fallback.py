"""Замыкающий резолвер: свободный диалог как обычный инструмент.

Если ни одно правило не сработало, реплика уходит в разговорный инструмент
``core.chat``. Это делает диалог таким же участником системы, как включение
света: у него есть имя, схема и результат — никаких особых путей в ядре.
"""

from __future__ import annotations

from jarvis.core.contracts import Intent, Utterance

#: Имя разговорного инструмента, который регистрирует ядро.
CHAT_TOOL = "core.chat"


class FallbackResolver:
    """Всегда возвращает намерение поговорить."""

    def __init__(self, *, tool: str = CHAT_TOOL, confidence: float = 0.3) -> None:
        self._tool = tool
        self._confidence = confidence

    @property
    def name(self) -> str:
        """Имя резолвера."""
        return "fallback"

    async def resolve(self, utterance: Utterance) -> Intent | None:
        """Отдать реплику разговорному инструменту."""
        if not utterance.text.strip():
            return None
        return Intent(
            tool=self._tool,
            # Язык передаётся дальше, чтобы модель ответила на языке вопроса.
            arguments={"text": utterance.text, "language": utterance.language},
            confidence=self._confidence,
            resolver=self.name,
            utterance=utterance.text,
        )
