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

from ..templates import compile_template as _compile
from ..templates import second_request
from ..templates import specificity as _specificity

logger = logging.getLogger(__name__)



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
        # Точная фраза тоже может уступить: «открой в картах» значит «открой
        # найденное место», только пока место свежее; без него это просто карты.
        if tool_name is not None and self._recognized(tool_name, {}):
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
            if not match:
                continue
            arguments = {k: v.strip() for k, v in match.groupdict().items() if v}
            if second_request(arguments):
                # В слот попала вторая просьба: «найди на ютубе видео Veritasium
                # и открой его» — `найди на {engine} {query}` забирал «видео … и
                # открой его» целиком в поисковый запрос (стенд 25.09.2026). Две
                # просьбы подряд, где вторая ссылается на первую, — работа для
                # плана, а не для одного инструмента.
                logger.debug("В слот %s попала вторая просьба — уступаю", name)
                continue
            if not self._recognized(name, arguments):
                # Шаблон совпал по форме, но инструмент такого значения не
                # знает: «открой в википедии статью про Тверь» — не программа.
                # Уступаем дальше: следующему шаблону, выученному, модели.
                logger.debug("Шаблон %s не узнал %s — уступаю", name, arguments)
                continue
            return Intent(
                tool=name,
                arguments=arguments,
                confidence=0.95,
                resolver=self.name,
                utterance=utterance.text,
            )
        return None

    def _recognized(self, name: str, arguments: Mapping[str, str]) -> bool:
        """Узнаёт ли инструмент значения из шаблона. Не объявил — узнаёт всё.

        Сломавшаяся проверка считается «узнал»: иначе ошибка в одном скилле
        отнимала бы у него все его фразы разом.
        """
        found = self._registry.get(name)
        recognizer = found.recognizer if found is not None else None
        if recognizer is None:
            return True
        try:
            return bool(recognizer(arguments))
        except Exception:  # noqa: BLE001 — проверка не важнее самой команды
            logger.exception("Проверка значения у %s упала — считаю узнанным", name)
            return True
