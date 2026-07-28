"""Намерение — результат работы роутера.

Роутер не решает, «какой скилл обработает запрос». Он превращает фразу в
конкретный вызов инструмента: имя плюс аргументы. Кто владеет инструментом —
знает только реестр (`ToolRegistry`), и это позволяет менять реализацию
маршрутизации (правила, эмбеддинги, LLM) без правок в остальных частях.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True, kw_only=True)
class Utterance:
    """Реплика пользователя — вход роутера."""

    text: str
    language: str = "ru"
    confidence: float = 1.0
    source: str = "voice"

    @property
    def cleaned(self) -> str:
        """Текст без лишних пробелов и хвостовой пунктуации, регистр сохранён.

        Из него извлекаются аргументы шаблонов: «запомни купить кабель XLR»
        должно сохранить в память «купить кабель XLR», а не «xlr».
        """
        return " ".join(self.text.split()).strip(" .,!?;:")

    @property
    def normalized(self) -> str:
        """`cleaned` в нижнем регистре — для сравнения фраз."""
        return self.cleaned.lower()


@dataclass(frozen=True, slots=True, kw_only=True)
class Intent:
    """Разобранное намерение: какой инструмент вызвать и с чем."""

    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    resolver: str = ""
    utterance: str = ""

    def with_resolver(self, resolver: str) -> "Intent":
        """Вернуть копию с проставленным именем резолвера."""
        return Intent(
            tool=self.tool,
            arguments=self.arguments,
            confidence=self.confidence,
            resolver=resolver,
            utterance=self.utterance,
        )
