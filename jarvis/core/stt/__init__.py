"""Распознавание речи: протокол, движки, заглушка."""

from __future__ import annotations

import logging
from dataclasses import replace
from importlib.util import find_spec

from jarvis.core.config import STTConfig
from jarvis.core.runtime import BlockingWorker

from .fallback import FallbackSTT
from .faster_whisper import FasterWhisperSTT
from .null import NullSTT
from .protocol import STT, Transcript

logger = logging.getLogger(__name__)


def _one(config: STTConfig, worker: BlockingWorker, engine: str) -> STT | None:
    """Собрать один движок по имени. ``None`` — не вышло, причина в логе."""
    if engine in ("", "null", "none"):
        return NullSTT()

    if engine == "faster-whisper":
        # Только наличие пакетов, без их загрузки: сам `faster_whisper` тянет
        # ctranslate2 и на холодном старте стоит десятки секунд, а нужен он
        # лишь при обрыве облака. Модель и так грузится лениво, в `start`.
        missing = [name for name in ("faster_whisper", "numpy") if find_spec(name) is None]
        if missing:
            logger.warning(
                "faster-whisper недоступен (нет %s). Установи: pip install 'jarvis-core[stt]'",
                ", ".join(missing),
            )
            return None
        return FasterWhisperSTT(config, worker)

    if engine == "deepgram":
        if not config.api_key:
            logger.warning(
                "Deepgram выбран, но ключа нет — задай JARVIS_DEEPGRAM_KEY в .env"
            )
            return None
        from .deepgram import DeepgramSTT

        return DeepgramSTT(config, api_key=config.api_key)

    logger.warning(
        "Неизвестный движок STT %r. Доступны: deepgram, faster-whisper, null", engine
    )
    return None


def build_stt(config: STTConfig, worker: BlockingWorker) -> STT:
    """Создать распознаватель по конфигу.

    Отсутствие зависимости или ключа — не повод падать: в худшем случае
    поднимается заглушка, а в лог уходит предупреждение.

    Когда задан `stt.fallback`, движки связываются в пару: основной работает,
    запасной ждёт отказа и поднимается только при нём. Не собрался основной —
    запасной становится единственным, и это правильнее, чем оставить систему
    вовсе без слуха: облако без ключа бесполезно, а местная модель работает
    всегда.
    """
    primary = _one(config, worker, config.engine)
    backup = None
    if config.fallback:
        # У запасного своя модель: имя облачной (`nova-3`) Whisper не знает, и
        # при обрыве сети запасной путь падал на загрузке (14.09.2026, 18:10).
        spare = config if config.fallback == config.engine else replace(config, model=config.fallback_model)
        backup = _one(spare, worker, config.fallback)

    if primary is None:
        if backup is None:
            return NullSTT()
        logger.warning(
            "Основное распознавание (%s) не собралось — работаю на запасном (%s)",
            config.engine,
            config.fallback,
        )
        return backup

    if backup is None or isinstance(backup, NullSTT):
        return primary
    return FallbackSTT(primary, backup, race_after_s=config.race_after_s)


__all__ = [
    "STT",
    "FallbackSTT",
    "FasterWhisperSTT",
    "NullSTT",
    "Transcript",
    "build_stt",
]
