"""Резолвер точных фраз — первое и самое дешёвое звено цепочки.

Скилл объявляет фразы прямо в декораторе инструмента, поэтому новый скилл
расширяет маршрутизацию сам: править ядро или конфиг не нужно.

Обычные команды студии («включи игровой режим», «какая температура») сюда
попадают и до сети не доходят — ноль токенов, мгновенный отклик.

Поддерживаются шаблоны с подстановкой: ``"включи {mode} режим"`` вытащит
``mode`` из реплики и передаст инструменту.
"""

from __future__ import annotations

import logging
import re
from typing import Mapping

from jarvis.core.contracts import Intent, Utterance
from jarvis.core.tools import ToolRegistry

logger = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _compile(phrase: str) -> re.Pattern[str] | None:
    """Собрать регулярку из шаблона вида ``включи {mode} режим``.

    Литеральные куски экранируются, плейсхолдеры превращаются в именованные
    группы. Экранировать фразу целиком нельзя: `re.escape` съест фигурные скобки.
    """
    if not _PLACEHOLDER.search(phrase):
        return None

    parts: list[str] = []
    cursor = 0
    for match in _PLACEHOLDER.finditer(phrase):
        parts.append(re.escape(phrase[cursor : match.start()]))
        parts.append(f"(?P<{match.group(1)}>.+?)")
        cursor = match.end()
    parts.append(re.escape(phrase[cursor:]))

    try:
        return re.compile(rf"^{''.join(parts)}$", re.IGNORECASE)
    except re.error:
        logger.warning("Некорректный шаблон фразы: %r", phrase)
        return None


def _specificity(phrase: str) -> int:
    """Сколько в шаблоне собственных букв, не считая подстановок."""
    return len(_PLACEHOLDER.sub("", phrase).replace(" ", ""))


class PhraseResolver:
    """Точное и шаблонное совпадение по фразам, объявленным скиллами."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        """Имя резолвера."""
        return "phrase"

    def _index(self) -> tuple[Mapping[str, str], list[tuple[re.Pattern[str], str]]]:
        """Собрать индексы точных фраз и шаблонов; каталог может меняться на лету.

        Шаблоны выстраиваются от частного к общему: у кого больше собственных
        слов, тот и проверяется первым. Иначе «найди в гугле котиков» досталось
        бы шаблону «найди {query}», и разбирать, какой скилл загрузился раньше,
        пришлось бы по алфавиту имён файлов.
        """
        exact: dict[str, str] = {}
        scored: list[tuple[int, re.Pattern[str], str]] = []
        for spec in self._registry.specs():
            for phrase in spec.phrases:
                normalized = " ".join(phrase.lower().split())
                compiled = _compile(normalized)
                if compiled is None:
                    exact[normalized] = spec.name
                else:
                    scored.append((_specificity(normalized), compiled, spec.name))
        scored.sort(key=lambda item: item[0], reverse=True)
        return exact, [(pattern, name) for _, pattern, name in scored]

    async def resolve(self, utterance: Utterance) -> Intent | None:
        """Найти инструмент по точной фразе или шаблону."""
        text = utterance.normalized
        if not text:
            return None

        exact, templates = self._index()

        tool_name = exact.get(text)
        if tool_name is not None:
            return Intent(
                tool=tool_name,
                confidence=1.0,
                resolver=self.name,
                utterance=utterance.text,
            )

        # Шаблоны применяются к тексту в исходном регистре: аргумент может быть
        # именем собственным или моделью оборудования, и портить его нельзя.
        for pattern, name in templates:
            match = pattern.match(utterance.cleaned)
            if match:
                return Intent(
                    tool=name,
                    arguments={k: v.strip() for k, v in match.groupdict().items() if v},
                    confidence=0.95,
                    resolver=self.name,
                    utterance=utterance.text,
                )
        return None
