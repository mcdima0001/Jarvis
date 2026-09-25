"""Шаблоны фраз: сборка регулярки и мера частности.

Вынесено из резолвера фраз, потому что тем же языком описываются два разных
набора: объявленные скиллами фразы и выученные формулировки, которые роутер
запомнил после удачного разбора моделью. Правила совпадения у них обязаны быть
одинаковыми — иначе выученная фраза начнёт вести себя не так, как та же самая,
записанная в скилле.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping

logger = logging.getLogger(__name__)

#: Подстановка вида ``{query}``.
PLACEHOLDER = re.compile(r"\{(\w+)\}")

#: Слово в шаблоне — то, что осталось за вычетом подстановок.
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def compile_template(phrase: str) -> re.Pattern[str] | None:
    """Собрать регулярку из шаблона вида ``включи {mode} режим``.

    Литеральные куски экранируются, подстановки превращаются в именованные
    группы. Экранировать фразу целиком нельзя: `re.escape` съест фигурные
    скобки, и шаблон перестанет быть шаблоном.

    :return: ``None``, если подстановок нет — такую фразу сравнивают буквально.
    """
    if not PLACEHOLDER.search(phrase):
        return None

    parts: list[str] = []
    cursor = 0
    for match in PLACEHOLDER.finditer(phrase):
        parts.append(re.escape(phrase[cursor : match.start()]))
        parts.append(f"(?P<{match.group(1)}>.+?)")
        cursor = match.end()
    parts.append(re.escape(phrase[cursor:]))

    try:
        return re.compile(rf"^{''.join(parts)}$", re.IGNORECASE)
    except re.error:
        logger.warning("Некорректный шаблон фразы: %r", phrase)
        return None


def specificity(phrase: str) -> int:
    """Сколько в шаблоне собственных букв, не считая подстановок."""
    return len(PLACEHOLDER.sub("", phrase).replace(" ", ""))


def literal_words(phrase: str) -> int:
    """Сколько в шаблоне собственных слов.

    Нужно там, где шаблон не написан человеком, а выведен автоматически:
    «включи {control}» с одним своим словом поймает слишком многое, а
    «поставь на паузу {site}» с тремя — почти ничего лишнего.
    """
    return len(_WORD.findall(PLACEHOLDER.sub(" ", phrase)))


#: Вторая просьба внутри слота: союз и повелительный глагол следом. Глаголы —
#: повелительные, а не любые: «включи трек Я сошла с ума и не помню» и «напиши
#: маме буду через час и куплю хлеб» — одна просьба, союз там живёт внутри.
_SECOND_REQUEST = re.compile(
    r"\sи\s+(?:потом\s+|затем\s+|ещё\s+|еще\s+)?"
    r"(?:открой|включи|найди|покажи|нажми|запусти|переведи|поставь|сделай|отправь|"
    r"закрой|выключи|перейди|зайди|скачай|сохрани|построй|посмотри|прочитай|прочти|"
    r"проложи|скопируй|вставь)\b",
    re.IGNORECASE,
)


def second_request(arguments: Mapping[str, Any]) -> bool:
    """Попала ли в слот вторая просьба — тогда разбирать фразу шаблоном нельзя.

    Стенд 25.09.2026: «найди на ютубе видео Veritasium и открой его» забирали
    по очереди разные резолверы — сперва шаблон фраз, потом `loose`, положивший
    в название канала «veritasium и открой его». Проверка одна на всех, кто
    заполняет слоты: иначе фраза просачивается через того, кого забыли.
    """
    return any(
        isinstance(value, str) and _SECOND_REQUEST.search(f" {value}") for value in arguments.values()
    )

