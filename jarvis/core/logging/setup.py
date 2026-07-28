"""Настройка стандартного `logging`: файл с ротацией плюс консоль."""

from __future__ import annotations

import logging
import logging.handlers
import sys

from jarvis.core.config import LoggingConfig

_FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)-28s %(message)s"
_CONSOLE_FORMAT = "%(levelname)-8s %(name)-24s %(message)s"

# Чей уровень задаёт конфиг. Всем остальным — не ниже WARNING.
#
# Список именно белый, а не чёрный: перечислять шумные библиотеки бесполезно,
# каждая новая зависимость приносит свои. На DEBUG numba вываливает в лог
# дизассемблер каждой функции, которую компилирует, — сотни строк на фразу,
# и в них тонет всё наше.
_APP_LOGGERS = ("jarvis",)


def setup_logging(config: LoggingConfig) -> logging.Logger:
    """Настроить корневой логгер и вернуть логгер приложения."""
    level = getattr(logging, config.level, logging.INFO)

    root = logging.getLogger()
    # Корень держим на WARNING: чужие логгеры уровня не имеют и берут его
    # у корня, поэтому DEBUG в конфиге иначе включает отладку всему, что
    # установлено в системе.
    root.setLevel(max(level, logging.WARNING))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    config.dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        config.dir / config.file,
        maxBytes=config.max_bytes,
        backupCount=config.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    root.addHandler(file_handler)

    if config.console:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
        root.addHandler(console)

    for name in _APP_LOGGERS:
        logging.getLogger(name).setLevel(level)

    return logging.getLogger("jarvis")
