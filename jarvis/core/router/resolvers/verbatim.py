"""Дословный захват: приставка-глагол, а дальше — свободный текст как есть.

Диктовка («впиши …», «набери …») ломает обычный разбор. Шаблон «впиши {text}»
спотыкается о запятую сразу после глагола, а модель, разобрав фразу, отдаёт не
весь хвост, а кусок до первой точки — и половина продиктованного теряется.
Живой случай: «впиши, что работает. Пусть даст доступ к LLM» превратилось в
«что работает».

Поэтому диктовка идёт мимо шаблонов и модели: узнали приставку — всё, что после
неё, кладём в аргумент **дословно**. Что делать с этим текстом дальше (набрать
как есть или переписать) — забота инструмента, а не роутера. Приставки и
инструмент заданы в конфиге (`router.verbatim`), поэтому ядро по-прежнему не
знает про скиллы по именам.
"""

from __future__ import annotations

import logging

from jarvis.core.config import VerbatimRule
from jarvis.core.contracts import Intent, Utterance

logger = logging.getLogger(__name__)

#: Что срезать с краёв глагола и хвоста: знаки, но не буквы.
_EDGES = " \t.,!?;:—-\"'«»"


class VerbatimResolver:
    """Приставка-глагол → остаток реплики дословным аргументом инструмента."""

    def __init__(self, rules: tuple[VerbatimRule, ...]) -> None:
        #: Слово-приставка → (инструмент, имя аргумента). Слова уже в нижнем
        #: регистре из конфига; на всякий случай приводим ещё раз.
        self._by_word: dict[str, tuple[str, str]] = {}
        for rule in rules:
            for word in rule.words:
                self._by_word[word.lower()] = (rule.tool, rule.arg)

    @property
    def name(self) -> str:
        """Имя резолвера."""
        return "verbatim"

    async def resolve(self, utterance: Utterance) -> Intent | None:
        """Если реплика начинается с приставки, отдать хвост дословно."""
        if not self._by_word:
            return None
        head, _, tail = utterance.cleaned.partition(" ")
        verb = head.lower().strip(_EDGES)
        target = self._by_word.get(verb)
        if target is None:
            return None
        body = tail.strip(_EDGES)
        if not body:
            # «Впиши» без текста — не диктовка, а повод переспросить. Пусть этим
            # займётся обычная цепочка, а не мы наугад.
            return None
        tool, arg = target
        logger.debug("Дословно: %r -> %s(%s=…%d симв.)", verb, tool, arg, len(body))
        return Intent(
            tool=tool,
            arguments={arg: body},
            confidence=1.0,
            resolver=self.name,
            utterance=utterance.text,
        )
