"""Точка входа: ``python -m jarvis``.

Режимы:

* без флагов — полный запуск (микрофон, скиллы, шина);
* ``--check`` — собрать систему и напечатать отчёт, ничего не запуская;
* ``--say "текст"`` — прогнать одну команду через роутер, минуя микрофон.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import replace
from pathlib import Path

from jarvis.core.app import JarvisApp
from jarvis.core.assets import download_voice, list_voices, preview_voices
from jarvis.core.audio import list_devices
from jarvis.core.config import DEFAULT_CONFIG_PATH, load_config
from jarvis.core.errors import AudioError, ConfigError, JarvisError
from jarvis.core.logging import setup_logging


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Разобрать аргументы командной строки."""
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="Jarvis — голосовой ассистент домашней студии",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"путь к конфигурации (по умолчанию {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="собрать систему, напечатать отчёт и выйти (без сети и моделей)",
    )
    parser.add_argument(
        "--say",
        metavar="ТЕКСТ",
        help="выполнить одну текстовую команду и выйти",
    )
    parser.add_argument(
        "--devices",
        action="store_true",
        help="показать звуковые устройства и выйти",
    )
    parser.add_argument(
        "--download-voice",
        metavar="ГОЛОС",
        nargs="?",
        const="",
        help="скачать голос Piper (без имени — показать список голосов)",
    )
    parser.add_argument(
        "--try-voice",
        metavar="ГОЛОС",
        nargs="+",
        help="послушать голоса и выбрать: имена через пробел либо ru / en",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="переопределить уровень логирования из конфига",
    )
    return parser.parse_args(argv)


async def _check(app: JarvisApp) -> int:
    """Загрузить скиллы, напечатать отчёт и погасить приложение."""
    await app.start()
    try:
        print(app.summary())
    finally:
        await app.stop("завершена проверка")
    return 0


async def _say(app: JarvisApp, text: str) -> int:
    """Выполнить одну команду и напечатать результат."""
    await app.start()
    try:
        result = await app.say(text)
        print()
        print(f"Команда:    {text}")
        print(f"Инструмент: {result.tool or '—'}")
        print(f"Итог:       {'успех' if result.ok else 'ошибка'} за {result.duration:.2f} с")
        if result.speech:
            print(f"Ответ:      {result.speech}")
        if result.value is not None and result.value != result.speech:
            print(f"Значение:   {result.value}")
        if result.error:
            print(f"Ошибка:     {result.error}")
    finally:
        await app.stop("команда выполнена")
    return 0 if result.ok else 1


async def _amain(args: argparse.Namespace) -> int:
    """Асинхронная часть запуска."""
    config = load_config(args.config)
    if args.log_level:
        config = replace(config, logging=replace(config.logging, level=args.log_level))

    logger = setup_logging(config.logging)
    logger.info("Конфигурация загружена: %s", config.source)

    # Служебные режимы не поднимают приложение целиком.
    if args.devices:
        print(list_devices())
        return 0
    if args.download_voice is not None:
        if not args.download_voice:
            print(list_voices())
            return 0
        download_voice(args.download_voice, config.tts.models_dir)
        return 0
    if args.try_voice:
        preview_voices(args.try_voice, config.tts.models_dir)
        return 0

    app = JarvisApp.build(config)

    if args.check:
        return await _check(app)
    if args.say:
        return await _say(app, args.say)

    await app.run()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Синхронная точка входа."""
    args = _parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        print("\nОстановлено пользователем")
        return 130
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    except AudioError as exc:
        print(f"Ошибка звука: {exc}", file=sys.stderr)
        return 3
    except JarvisError as exc:
        logging.getLogger("jarvis").error("Ошибка: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
