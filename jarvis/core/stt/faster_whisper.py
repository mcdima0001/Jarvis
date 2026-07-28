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

    def _resolve_device(self) -> tuple[str, str]:
        """Определить устройство и тип вычислений.

        При ``device: auto`` проверяется наличие CUDA: на видеокарте выгоднее
        float16, на процессоре — int8. Так один и тот же конфиг работает и на
        ноутбуке, и на студийном ПК с GTX.
        """
        device = self._config.device
        compute = self._config.compute_type

        if device == "auto":
            try:
                import ctranslate2

                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception as exc:  # noqa: BLE001 — проверка не должна ронять старт
                logger.debug("Не удалось определить наличие CUDA (%s), беру cpu", exc)
                device = "cpu"

        if compute in ("", "auto"):
            compute = "float16" if device == "cuda" else "int8"
        return device, compute

    async def start(self) -> None:
        """Загрузить модель Whisper в отдельном потоке."""
        if self._model is not None:
            return
        device, compute = self._resolve_device()
        logger.info(
            "Загружаю Whisper: модель=%s устройство=%s тип=%s",
            self._config.model,
            device,
            compute,
        )
        self._model = await self._worker.run(self._load, device, compute)
        logger.info("Whisper готов")

    def _load(self, device: str, compute_type: str) -> Any:
        """Синхронная загрузка — выполняется в пуле потоков."""
        from faster_whisper import WhisperModel

        self._config.models_dir.mkdir(parents=True, exist_ok=True)
        try:
            return WhisperModel(
                self._config.model,
                device=device,
                compute_type=compute_type,
                download_root=str(self._config.models_dir),
            )
        except Exception as exc:
            if device == "cuda":
                logger.warning(
                    "Не удалось поднять Whisper на видеокарте (%s) — перехожу на процессор",
                    exc,
                )
                return WhisperModel(
                    self._config.model,
                    device="cpu",
                    compute_type="int8",
                    download_root=str(self._config.models_dir),
                )
            raise

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
            # Смещает словарь модели в сторону имени ассистента и названий
            # программ студии: без этого «Джарвис» превращается в «жаркость».
            initial_prompt=self._config.initial_prompt or None,
        )
        parts = [segment.text for segment in segments]
        text = " ".join(part.strip() for part in parts).strip()
        return Transcript(
            text=text,
            language=getattr(info, "language", self._config.language),
            confidence=float(getattr(info, "language_probability", 1.0)),
            duration=float(getattr(info, "duration", 0.0)),
        )
