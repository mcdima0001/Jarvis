"""Намерение — результат работы роутера.

Роутер не решает, «какой скилл обработает запрос». Он превращает фразу в
конкретный вызов инструмента: имя плюс аргументы. Кто владеет инструментом —
знает только реестр (`ToolRegistry`), и это позволяет менять реализацию
маршрутизации (правила, эмбеддинги, LLM) без правок в остальных частях.
"""

from __future__ import annotations

import re
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


#: Числительные словами. Whisper пишет числа то цифрами, то прописью, и «сделай
#: громкость пятьдесят» должно работать так же, как «громкость 50».
_SPOKEN_NUMBERS: dict[str, int] = {
    "ноль": 0, "один": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40,
    "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80,
    "девяносто": 90, "сто": 100,
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90, "hundred": 100,
}

_DIGITS = re.compile(r"-?\d+(?:[.,]\d+)?")


def parse_number(text: str) -> float | None:
    """Достать число из услышанного.

    Речь приходит с лишними словами: шаблон «громкость {level}» на фразе
    «громкость на 50» отдаёт в аргумент «на 50», а не «50». Требовать от
    человека говорить ровно как в шаблоне бессмысленно, поэтому число
    вытаскивается из того, что есть.

    Возвращает ``None``, если числа нет или их несколько: угадывать, какое из
    них имелось в виду, опаснее, чем переспросить.
    """
    digits = _DIGITS.findall(text)
    if len(digits) == 1:
        return float(digits[0].replace(",", "."))
    if digits:
        return None

    # Числительные словами складываются: «двадцать пять» — это 25.
    words = [_SPOKEN_NUMBERS[word] for word in re.findall(r"[^\W\d_]+", text.lower())
             if word in _SPOKEN_NUMBERS]
    return float(sum(words)) if words else None


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
