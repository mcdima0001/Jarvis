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
