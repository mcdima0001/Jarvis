"""Адаптер faster-whisper.

faster-whisper синхронный и CPU-bound. Объявить метод `async` и вызвать внутри
`model.transcribe()` значило бы заморозить весь event loop на время
распознавания — поэтому и загрузка модели, и сам вызов уходят в
`BlockingWorker`.

Если пакет не установлен или модель не скачалась, приложение не падает:
composition root поднимет `NullSTT` и напишет предупреждение.
"""

from __future__ import annotations

import logging
from typing import Any

from jarvis.core.config import STTConfig
from jarvis.core.runtime import BlockingWorker

from .protocol import Transcript

logger = logging.getLogger(__name__)

#: Whisper ждёт float32 в диапазоне [-1, 1]; PCM16 приходит целыми.
_PCM16_SCALE = 32768.0


class FasterWhisperSTT:
    """Распознавание речи через faster-whisper."""

    def __init__(self, config: STTConfig, worker: BlockingWorker) -> None:
        self._config = config
        self._worker = worker
        self._model: Any = None

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "stt"

    @property
    def ready(self) -> bool:
        """Загружена ли модель."""
        return self._model is not None

    async def start(self) -> None:
        """Загрузить модель Whisper в отдельном потоке."""
        if self._model is not None:
            return
        logger.info(
            "Загружаю Whisper: модель=%s устройство=%s тип=%s",
            self._config.model,
            self._config.device,
            self._config.compute_type,
        )
        self._model = await self._worker.run(self._load)
        logger.info("Whisper готов")

    def _load(self) -> Any:
        """Синхронная загрузка — выполняется в пуле потоков."""
        from faster_whisper import WhisperModel

        self._config.models_dir.mkdir(parents=True, exist_ok=True)
        return WhisperModel(
            self._config.model,
            device=self._config.device,
            compute_type=self._config.compute_type,
            download_root=str(self._config.models_dir),
        )

    async def stop(self) -> None:
        """Освободить модель."""
        self._model = None

    async def transcribe(self, audio: bytes, *, sample_rate: int = 16000) -> Transcript:
        """Распознать моно-PCM 16 бит."""
        if self._model is None:
            raise RuntimeError("Whisper не загружен — вызови start()")
        if not audio:
            return Transcript(text="")
        return await self._worker.run(self._transcribe, audio)

    def _transcribe(self, audio: bytes) -> Transcript:
        """Синхронное распознавание — выполняется в пуле потоков."""
        import numpy as np

        samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / _PCM16_SCALE
        segments, info = self._model.transcribe(
            samples,
            language=self._config.language or None,
            beam_size=self._config.beam_size,
            vad_filter=False,
        )
        parts = [segment.text for segment in segments]
        text = " ".join(part.strip() for part in parts).strip()
        return Transcript(
            text=text,
            language=getattr(info, "language", self._config.language),
            confidence=float(getattr(info, "language_probability", 1.0)),
            duration=float(getattr(info, "duration", 0.0)),
        )
