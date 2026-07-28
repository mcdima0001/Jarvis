"""Распознавание речи: протокол, faster-whisper, заглушка."""

from __future__ import annotations

import logging

from jarvis.core.config import STTConfig
from jarvis.core.runtime import BlockingWorker

from .faster_whisper import FasterWhisperSTT
from .null import NullSTT
from .protocol import STT, Transcript

logger = logging.getLogger(__name__)


def build_stt(config: STTConfig, worker: BlockingWorker) -> STT:
    """Создать распознаватель по конфигу.

    Отсутствие зависимости — не повод падать: возвращается заглушка,
    а в лог уходит предупреждение.
    """
    if config.engine in (None, "", "null"):
        return NullSTT()

    if config.engine == "faster-whisper":
        try:
            import faster_whisper  # noqa: F401
            import numpy  # noqa: F401
        except ImportError as exc:
            logger.warning(
                "faster-whisper недоступен (%s) — распознавание отключено. "
                "Установи: pip install 'jarvis-core[stt]'",
                exc,
            )
            return NullSTT()
        return FasterWhisperSTT(config, worker)

    logger.warning("Неизвестный движок STT %r — использую заглушку", config.engine)
    return NullSTT()


__all__ = ["STT", "FasterWhisperSTT", "NullSTT", "Transcript", "build_stt"]
