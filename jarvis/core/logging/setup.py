"""Настройка стандартного `logging`: файл с ротацией плюс консоль."""

from __future__ import annotations

import logging
import logging.handlers
import sys

from jarvis.core.config import LoggingConfig

_FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)-28s %(message)s"
_CONSOLE_FORMAT = "%(levelname)-8s %(name)-24s %(message)s"

# Библиотеки, которые иначе засоряют DEBUG-вывод.
_NOISY = ("httpx", "httpcore", "urllib3", "asyncio", "faster_whisper")


def setup_logging(config: LoggingConfig) -> logging.Logger:
    """Настроить корневой логгер и вернуть логгер приложения."""
    level = getattr(logging, config.level, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
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

    for name in _NOISY:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))

    return logging.getLogger("jarvis")
