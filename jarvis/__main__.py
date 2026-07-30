"""Точка входа: ``python -m jarvis``.

Режимы:

* без флагов — полный запуск (микрофон, скиллы, шина);
* ``--check`` — собрать систему и напечатать отчёт, ничего не запуская;
* ``--say "текст"`` — прогнать одну команду через роутер, минуя микрофон.
  Уши при этом не поднимаются: команда уже написана, распознавать нечего.
  С ``--no-voice`` не поднимается и синтез — ответ только печатается.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import replace
from pathlib import Path

from jarvis.core.app import JarvisApp
from jarvis.core.assets import download_voice, list_voices, make_reference, preview_voices
from jarvis.core.audio import list_devices
from jarvis.core.config import DEFAULT_CONFIG_PATH, JarvisConfig, load_config
from jarvis.core.contracts import detect_language
from jarvis.core.errors import AudioError, ConfigError, JarvisError, STTError
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
        help="выполнить одну текстовую команду и выйти (без микрофона и распознавания)",
    )
    parser.add_argument(
        "--no-voice",
        action="store_true",
        help="с --say: не поднимать синтез, ответ только напечатать",
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
        help="послушать голоса и выбрать: имена через пробел либо jarvis / ru / en / clone",
    )
    parser.add_argument(
        "--make-reference",
        metavar="ГОЛОС",
        help="снять эталон, чтобы XTTS говорил этим голосом на втором языке: "
        "движок:голос (vosk:male_0) либо путь к записи",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="переопределить уровень логирования из конфига",
    )
    return parser.parse_args(argv)


async def _check(app: JarvisApp) -> int:
    """Загрузить скиллы, напечатать отчёт и погасить приложение.

    Модели не поднимаются: отчёт о конфиге, скиллах и каталоге инструментов
    в них не нуждается, а Whisper с Vosk грузятся минуты.
    """
    await app.start(ears=False, voice=False)
    try:
        print(app.summary())
    finally:
        await app.stop("завершена проверка")
    return 0


async def _say(app: JarvisApp, text: str, *, voice: bool = True) -> int:
    """Выполнить одну команду и напечатать результат.

    Уши не поднимаются: команда пришла текстом, распознавать нечего. Раньше
    ради одной такой команды грузился Whisper и открывался микрофон.
    """
    await app.start(ears=False, voice=voice)
    try:
        result = await app.say(text)
        # Печатаем **сказанное**, а не догадку о нём: вариант реплики выбирает
        # персона, и `speech_for` вернул бы первый из набора — то есть в консоли
        # оказалось бы одно, а в колонках другое.
        spoken = app.pipeline.last_reply or result.speech_for(
            detect_language(text, default=app.config.app.language)
        )
        print()
        print(f"Команда:    {text}")
        print(f"Инструмент: {result.tool or '—'}")
        print(f"Итог:       {'успех' if result.ok else 'ошибка'} за {result.duration:.2f} с")
        if spoken:
            print(f"Ответ:      {spoken}")
        if result.value is not None and result.value != spoken:
            print(f"Значение:   {result.value}")
        if result.error:
            print(f"Ошибка:     {result.error}")
    finally:
        await app.stop("команда выполнена")
    return 0 if result.ok else 1


def _run_utility(args: argparse.Namespace, config: JarvisConfig) -> int | None:
    """Выполнить служебный режим, если он запрошен.

    Эти команды не поднимают приложение и работают **до** запуска event loop:
    они синхронные, а часть из них внутри сама обращается к асинхронным
    клиентам. Если запускать их из корутины, такой клиент упирается в уже
    работающую петлю.

    :return: код возврата, либо ``None``, если служебный режим не запрошен.
    """
    if args.devices:
        print(list_devices())
        return 0
    if args.download_voice is not None:
        if not args.download_voice:
            print(list_voices())
            return 0
        download_voice(args.download_voice, config.tts.models_dir)
        return 0
    if args.make_reference:
        make_reference(args.make_reference, config.tts.models_dir)
        return 0
    if args.try_voice:
        preview_voices(args.try_voice, config.tts.models_dir)
        return 0
    return None


async def _amain(config: JarvisConfig, args: argparse.Namespace) -> int:
    """Асинхронная часть запуска: поднимает приложение."""
    app = JarvisApp.build(config)

    if args.check:
        return await _check(app)
    if args.say:
        return await _say(app, args.say, voice=not args.no_voice)

    await app.run()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Синхронная точка входа."""
    args = _parse_args(argv)
    try:
        config = load_config(args.config)
        if args.log_level:
            config = replace(config, logging=replace(config.logging, level=args.log_level))

        logger = setup_logging(config.logging)
        logger.info("Конфигурация загружена: %s", config.source)

        utility = _run_utility(args, config)
        if utility is not None:
            return utility

        return asyncio.run(_amain(config, args))
    except KeyboardInterrupt:
        print("\nОстановлено пользователем")
        return 130
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    except AudioError as exc:
        print(f"Ошибка звука: {exc}", file=sys.stderr)
        return 3
    except STTError as exc:
        # Без распознавания голосом командовать нечем, поэтому запуск
        # прекращается. Но текст ошибки должен объяснять, что делать, — стек
        # ctranslate2 тут только пугает.
        print(f"Распознавание речи не поднялось: {exc}", file=sys.stderr)
        return 4
    except JarvisError as exc:
        logging.getLogger("jarvis").error("Ошибка: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
