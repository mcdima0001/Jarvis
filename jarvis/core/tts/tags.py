"""Теги манеры речи: ``[sad]``, ``[laughing]``, ``[whispering]``.

Облачный голос Fish читает их как указание, **как** сказать фразу, а не как
текст: с тегами ответ звучит живее (просьба владельца 20.10.2026). Набор не
закрыт — тег пишется словами, и движок понимает любой.

Остальные движки тегов не знают и прочли бы их вслух («сэд»), поэтому для них
теги срезаются. Решает это одно место — `CompositeTTS`, — а не автор реплики:
модель пишет теги, когда голос их понимает, но реплика может уйти и на
запасной голос, если облако отказало.
"""

from __future__ import annotations

import re

#: Тег — слова в квадратных скобках, и начинается он с буквы. Сноски из
#: Википедии («[1]», «[источник?]» — та начинается с буквы, но вопросительного
#: знака в теге нет) тегами не считаются: голос им не указ.
TAG = re.compile(r"\[[^\W\d_][\w '\-]{0,40}\]")


def strip_tags(text: str) -> str:
    """Убрать теги, сохранив текст вокруг: для голоса, который их не знает."""
    if "[" not in text:
        return text
    stripped = TAG.sub(" ", text)
    stripped = re.sub(r"[ \t]{2,}", " ", stripped)
    return re.sub(r" +([.,!?;:])", r"\1", stripped).strip()


def split_tags(text: str) -> list[tuple[str, bool]]:
    """Разрезать реплику на куски: ``(кусок, это ли тег)``.

    Нужно, чтобы готовить к синтезу только текст: подготовка переводит латиницу
    в кириллицу и выбрасывает скобки, и тег без защиты превратился бы в слово.
    """
    parts: list[tuple[str, bool]] = []
    position = 0
    for match in TAG.finditer(text):
        if match.start() > position:
            parts.append((text[position : match.start()], False))
        parts.append((match.group(0), True))
        position = match.end()
    if position < len(text):
        parts.append((text[position:], False))
    return parts
