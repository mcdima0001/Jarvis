"""Настройка стандартного `logging`: файл с ротацией плюс консоль.

**Файл подробнее консоли.** Уровень тут не один, а два: консоль читают глазами
по ходу дела, и лишние строки в ней мешают; файл читают, когда уже что-то пошло
не так, — и нужны в нём как раз те записи, которых не ждали.

Идея владельца, и она чинит целый класс промахов. До этого «на всякий случай»
писалось через `logger.info`, а раз строка попадала на глаза каждый день, у неё
появлялось условие «писать, только если есть что показать». На живом разборе
именно такое условие и обнулило диагностику: страница отвечала «я ничего не
вижу», условие «есть что показать» не выполнялось, и четыре попытки подряд не
оставили в логе ни строки. Со стороны выглядело, будто ассистент молча передумал.
Теперь подробности пишутся в `debug` без всяких условий: консоли они не мешают,
а в файле есть всегда.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import time

from jarvis.core.config import LoggingConfig

from .colors import ColorFormatter, enable_windows_colors, supports_color
from .daily import DailyFileHandler

#: Время стоит первым и в файле, и в консоли. Без него запись бесполезна для
#: разбора: почти всё, что приходится выяснять по логу, — это «что было
#: раньше, а что позже» и «сколько заняло».
_FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)-28s %(message)s"
_CONSOLE_FORMAT = "%(asctime)s %(levelname)-8s %(name)-24s %(message)s"

# Чей уровень задаёт конфиг. Всем остальным — не ниже WARNING.
#
# Список именно белый, а не чёрный: перечислять шумные библиотеки бесполезно,
# каждая новая зависимость приносит свои. На DEBUG numba вываливает в лог
# дизассемблер каждой функции, которую компилирует, — сотни строк на фразу,
# и в них тонет всё наше. С двумя уровнями это стало важнее, а не менее важно:
# подробный файл включён по умолчанию, и без белого списка он бы состоял из
# чужой отладки.
_APP_LOGGERS = ("jarvis",)


def _level(name: str, fallback: int) -> int:
    """Уровень по имени из конфига; опечатка не должна ронять запуск."""
    value = getattr(logging, str(name).upper(), None)
    return value if isinstance(value, int) else fallback


def setup_logging(config: LoggingConfig) -> logging.Logger:
    """Настроить корневой логгер и вернуть логгер приложения."""
    console_level = _level(config.level, logging.INFO)
    file_level = _level(config.file_level, logging.DEBUG)

    root = logging.getLogger()
    # Корень держим на WARNING: чужие логгеры уровня не имеют и берут его
    # у корня, поэтому подробность в конфиге иначе включает отладку всему,
    # что установлено в системе.
    root.setLevel(logging.WARNING)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    config.dir.mkdir(parents=True, exist_ok=True)
    # Один файл — один день: `jarvis-2026-07-30.log`. Резать по размеру нельзя —
    # границы файлов приходятся на случайные моменты, и «покажи, что было вчера»
    # превращается в поиск по времени внутри файла.
    file_handler = DailyFileHandler(
        config.dir / config.file, keep_days=config.keep_days
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt=config.time_format))
    root.addHandler(file_handler)

    if config.console:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(console_level)
        # Цвет — свойство консоли, и только её. Форматтер с раскраской некуда
        # поставить в файл: он вешается ровно на этот обработчик.
        if supports_color(console.stream, config.color):
            enable_windows_colors()
            console.setFormatter(ColorFormatter(datefmt=config.time_format))
        else:
            console.setFormatter(
                logging.Formatter(_CONSOLE_FORMAT, datefmt=config.time_format)
            )
        root.addHandler(console)

    # Логгер должен пропускать самое подробное из двух: он решает **первым**, и
    # то, что он отсёк, до обработчиков уже не дойдёт. Кто из них что покажет —
    # дело их собственных уровней.
    for name in _APP_LOGGERS:
        logging.getLogger(name).setLevel(min(console_level, file_level))

    app = logging.getLogger("jarvis")
    # Где начинается этот запуск. За день их бывает десяток, и без отметки
    # начало приходится искать по времени — а владелец однажды из-за этого решил,
    # что файл перезаписывается каждый сеанс.
    app.info("%s Запуск %s %s", "─" * 24, time.strftime(config.time_format), "─" * 24)
    return app
