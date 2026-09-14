"""Что из файла лога показывать глазами: то же, что видно в консоли.

Файл пишется подробнее консоли (`file_level: DEBUG`), и это правильно — его
читают, когда что-то сломалось. Но окно «Показать лог» в трее и вкладка «Лог»
в панели — это замена консоли, а не разбор аварии: владелец смотрит в них по
ходу дела, и отладочные строки топят то, ради чего он смотрит (просьба
14.09.2026). Поэтому показывается уровень консоли, `logging.level`.

Запись лога бывает многострочной (стек исключения, длинная реплика): строки
продолжения уровня не имеют и наследуют его у своей записи.
"""

from __future__ import annotations

import re

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

#: Начало записи: время (цифры и разделители) и уровень. Шаблон годится и для
#: Python, и для PowerShell — окно трея фильтрует им же.
RECORD = r"^\d[\d.:, /-]* (DEBUG|INFO|WARNING|ERROR|CRITICAL) "
_RECORD = re.compile(RECORD)


def visible_levels(level: str) -> tuple[str, ...]:
    """Уровни не ниже заданного; опечатка в конфиге — как INFO."""
    name = str(level).upper()
    return LEVELS[LEVELS.index(name) if name in LEVELS else 1 :]


def console_view(text: str, level: str) -> str:
    """Оставить в куске лога только то, что показала бы консоль."""
    shown = set(visible_levels(level))
    keep = True
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        match = _RECORD.match(line)
        if match:
            keep = match.group(1) in shown
        if keep:
            out.append(line)
    return "".join(out)
