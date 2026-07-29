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
        #: Фильтр тишины можно потерять на ходу: он требует onnxruntime, и на
        #: свежем Python колёс может ещё не быть.
        self._vad_filter = config.vad_filter

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

                found = ctranslate2.get_cuda_device_count()
            except Exception as exc:  # noqa: BLE001 — проверка не должна ронять старт
                logger.warning(
                    "Не удалось спросить про видеокарту (%s) — работаю на процессоре", exc
                )
                found = 0

            device = "cuda" if found > 0 else "cpu"
            if not found:
                # Раньше это писалось в debug, и молчаливый переход на
                # процессор выглядел как «видеокарта не нужна». Между тем
                # разница в скорости — раз в пять, а причина обычно одна:
                # ctranslate2 не видит библиотеки CUDA.
                logger.info(
                    "Видеокарта для расчётов не найдена — работаю на процессоре. "
                    "Whisper умеет считать только на CUDA, то есть на NVIDIA; "
                    "встроенная графика Intel и карты AMD ему недоступны. Есть ли "
                    "NVIDIA, показывает команда nvidia-smi. Если она есть, но "
                    "устройств ноль — не хватает библиотек: pip install "
                    "nvidia-cublas-cu12 nvidia-cudnn-cu12"
                )

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
                cpu_threads=self._config.cpu_threads,
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
                    cpu_threads=self._config.cpu_threads,
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

    def _pick_language(self, samples: Any) -> tuple[str, str | None]:
        """Выбрать язык распознавания и подсказку словаря для него.

        Автоопределение на коротких фразах ненадёжно: одиночное «Джарвис»
        Whisper относит к английскому с уверенностью 0.27 и слышит «I miss».
        Поэтому определение принимается, только если язык входит в разрешённые
        и модель в нём достаточно уверена; иначе берётся основной язык.
        """
        config = self._config
        if not config.auto_detect:
            return config.language, config.prompt_for(config.language)

        try:
            detected, probability, _ = self._model.detect_language(samples)
        except Exception as exc:  # noqa: BLE001 — определение не должно ронять распознавание
            logger.debug("Определить язык не удалось (%s), беру основной", exc)
            return config.fallback_language, config.prompt_for(config.fallback_language)

        if detected in config.languages and probability >= config.language_min_probability:
            logger.debug("Язык определён: %s (%.2f)", detected, probability)
            return detected, config.prompt_for(detected)

        logger.debug(
            "Язык не определён уверенно (%s, %.2f при пороге %.2f) — беру %s",
            detected,
            probability,
            config.language_min_probability,
            config.fallback_language,
        )
        return config.fallback_language, config.prompt_for(config.fallback_language)

    def _transcribe(self, audio: bytes) -> Transcript:
        """Синхронное распознавание — выполняется в пуле потоков."""
        import numpy as np

        samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / _PCM16_SCALE
        language, prompt = self._pick_language(samples)

        try:
            segments, info = self._model.transcribe(
                samples,
                language=language,
                beam_size=self._config.beam_size,
                # Тишину и не-речь выбрасывает Silero до самой модели. На
                # процессоре это главный рычаг: посторонний звук из колонок
                # перестаёт занимать распознавание целиком.
                vad_filter=self._vad_filter,
                # Смещает словарь модели в сторону имени ассистента и названий
                # программ студии: без этого «Джарвис» превращается в «жаркость».
                initial_prompt=prompt,
            )
        except Exception as exc:  # noqa: BLE001 — без фильтра работать можно
            if not self._vad_filter:
                raise
            logger.warning(
                "Фильтр тишины не работает (%s) — выключаю его до перезапуска. "
                "Обычно не хватает onnxruntime",
                exc,
            )
            self._vad_filter = False
            segments, info = self._model.transcribe(
                samples,
                language=language,
                beam_size=self._config.beam_size,
                vad_filter=False,
                initial_prompt=prompt,
            )
        try:
            parts = [segment.text for segment in segments]
        except RuntimeError as exc:
            # Whisper выделяет память лениво, уже при переборе сегментов, и на
            # загруженной машине падает именно здесь («mkl_malloc: failed to
            # allocate memory»). Полный стек тут ничего не объясняет, а фразу
            # всё равно не спасти — сообщаем причину и слушаем дальше.
            logger.warning(
                "Не хватило памяти на распознавание (%s). Если повторяется — "
                "возьми модель полегче: stt.model: base",
                exc,
            )
            return Transcript(text="", language=language or "", confidence=0.0)

        text = " ".join(part.strip() for part in parts).strip()
        return Transcript(
            text=text,
            language=getattr(info, "language", language) or language,
            confidence=float(getattr(info, "language_probability", 1.0)),
            duration=float(getattr(info, "duration", 0.0)),
        )
