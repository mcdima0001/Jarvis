"""Цвет в консоли — и только в консоли.

Лог читают в двух совершенно разных ситуациях. В консоль смотрят живьём, пока
ассистент работает, и там глаз ищет три вещи: что он услышал, что ответил и где
сломался. Файл читают потом, и в нём цвет — чистый вред: невидимые управляющие
последовательности ломают и `grep`, и глаза, и любой разбор.

Отсюда устройство: раскраска живёт в форматтере, а форматтер вешается **только**
на консольный обработчик. Не «не забыть отключить для файла», а «в файл его
физически некуда поставить».

**Цветом сказано ровно то, что нельзя перепутать.** Уровень — красным и жёлтым;
подробности — приглушённо, чтобы не мешали; время и имя логгера — тоже
приглушённо, потому что смотрят не на них. Остальные строки остаются обычными:
если раскрасить всё, не выделено ничего.

Отдельно помечаются реплики разговора: услышанное и сказанное. Пометка идёт
через ``extra={"tone": ...}`` — явным полем записи, а не угадыванием по тексту
сообщения. Угадывание рассыпается при первой же правке формулировки.
"""

from __future__ import annotations

import logging
import os
import sys

#: Управляющие последовательности. Свои, без зависимости: ради восьми цветов
#: тащить пакет на боевую машину незачем.
RESET = "\033[0m"
_CODES = {
    "dim": "\033[2m",
    "red": "\033[31m",
    "bright_red": "\033[91m",
    "yellow": "\033[33m",
    "green": "\033[32m",
    "cyan": "\033[36m",
}

#: Цвет по уровню. INFO намеренно без цвета: таких строк большинство, и
#: подсветить их значит не подсветить ничего.
_BY_LEVEL = {
    logging.DEBUG: "dim",
    logging.WARNING: "yellow",
    logging.ERROR: "red",
    logging.CRITICAL: "bright_red",
}

#: Цвет по пометке в записи (``extra={"tone": ...}``).
_BY_TONE = {
    "heard": "cyan",   # что ассистент услышал
    "said": "green",   # что ассистент ответил
}


def paint(text: str, color: str) -> str:
    """Обернуть текст в цвет. Незнакомый цвет — вернуть как есть."""
    code = _CODES.get(color)
    return f"{code}{text}{RESET}" if code else text


def supports_color(stream: object, mode: str = "auto") -> bool:
    """Стоит ли красить вывод в этот поток.

    :param mode: ``always`` — красить всегда, ``never`` — никогда, ``auto`` —
        по обстановке.

    В режиме ``auto`` цвет включается только для живого терминала. Если вывод
    перенаправлен в файл или уходит в другую программу, управляющие
    последовательности там окажутся мусором — а перенаправляют вывод как раз
    тогда, когда собираются его читать.

    ``NO_COLOR`` уважаем: это общее соглашение, и человек, который его выставил,
    уже объяснил, чего хочет.
    """
    if mode == "never":
        return False
    if mode == "always":
        return True
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — поток без isatty цвета не заслуживает
        return False


def enable_windows_colors() -> None:
    """Разрешить управляющие последовательности в консоли Windows.

    Без этого PowerShell печатает их буквально — ``←[31m`` вместо красного.
    Поддержка в консоли есть с Windows 10, но по умолчанию выключена, и
    включается она одним флагом. Молчаливый отказ тут правильный: не вышло —
    значит просто не будет цвета.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # -11 — стандартный вывод, 0x0004 — ENABLE_VIRTUAL_TERMINAL_PROCESSING.
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:  # noqa: BLE001 — цвет не стоит падения при запуске
        pass


#: Ширина колонок. Выравнивание делаем сами: `%(levelname)-8s` считает длину
#: вместе с управляющими последовательностями, и раскрашенная строка съезжает.
_LEVEL_WIDTH = 8
_NAME_WIDTH = 24


class ColorFormatter(logging.Formatter):
    """Форматтер консоли: раскрашивает уровень, время, имя и саму реплику.

    Красится не вся строка целиком: время и имя логгера приглушаются, а цвет
    достаётся тому, на что смотрят — уровню и тексту сообщения. Иначе жёлтая
    строка предупреждения читается хуже обычной.

    Строка собирается вручную, а не шаблоном с ширинами полей: `%(name)-24s`
    считает длину вместе с невидимыми управляющими символами, и колонки
    разъезжаются ровно у тех строк, которые раскрашены.
    """

    def format(self, record: logging.LogRecord) -> str:
        level = _BY_LEVEL.get(record.levelno)
        tone = _BY_TONE.get(str(getattr(record, "tone", "")))

        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        if level or tone:
            message = paint(message, tone or level or "")

        head = paint(self.formatTime(record, self.datefmt), "dim")
        name = paint(record.name.ljust(_NAME_WIDTH), "dim")
        shown = record.levelname.ljust(_LEVEL_WIDTH)
        return f"{head} {paint(shown, level) if level else shown} {name} {message}"
