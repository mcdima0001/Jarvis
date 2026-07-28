"""Намерение — результат работы роутера.

Роутер не решает, «какой скилл обработает запрос». Он превращает фразу в
конкретный вызов инструмента: имя плюс аргументы. Кто владеет инструментом —
знает только реестр (`ToolRegistry`), и это позволяет менять реализацию
маршрутизации (правила, эмбеддинги, LLM) без правок в остальных частях.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


def detect_language(text: str, *, default: str = "ru") -> str:
    """Определить язык текста по алфавиту.

    Для голоса язык приходит от Whisper, а у текстового ввода (``--say``,
    Telegram) его никто не сообщает. Считать буквы дешевле и надёжнее, чем
    гадать: русский и английский используют разные алфавиты, и этого хватает.

    :param text: реплика пользователя.
    :param default: что вернуть, если букв нет вовсе.
    """
    cyrillic = sum(1 for char in text if "а" <= char.lower() <= "я" or char.lower() == "ё")
    latin = sum(1 for char in text if "a" <= char.lower() <= "z")
    if not cyrillic and not latin:
        return default
    return "ru" if cyrillic >= latin else "en"


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
