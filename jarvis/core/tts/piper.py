"""Адаптер Piper TTS — локальный синтез с минимальной задержкой.

Piper синхронный: и загрузка голоса, и синтез уходят в `BlockingWorker`, иначе
на время генерации встанет весь event loop.

Воспроизведение делегируется `AudioSink`, чтобы синтез и вывод звука можно было
заменять по отдельности.

API Piper: `PiperVoice.load(path)` и `voice.synthesize(text, syn_config)`,
возвращающий поток `AudioChunk` с полями `audio_int16_bytes` и `sample_rate`.
Частота дискретизации берётся из самого голоса, а не из конфига: у модели она
фиксирована (у русских голосов 22050 Гц), и подменять её значило бы менять
скорость речи.
"""

from __future__ import annotations

import logging
from typing import Any

from jarvis.core.audio import AudioSink
from jarvis.core.config import TTSConfig
from jarvis.core.runtime import BlockingWorker

from .protocol import Speech

logger = logging.getLogger(__name__)


class PiperTTS:
    """Синтез речи через Piper."""

    def __init__(
        self,
        config: TTSConfig,
        worker: BlockingWorker,
        *,
        sink: AudioSink,
    ) -> None:
        self._config = config
        self._worker = worker
        self._sink = sink
        self._voice: Any = None
        self._sample_rate = config.sample_rate

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "tts"

    @property
    def ready(self) -> bool:
        """Загружен ли голос."""
        return self._voice is not None

    @property
    def model_path(self):
        """Путь к файлу голоса."""
        return self._config.models_dir / f"{self._config.voice}.onnx"

    async def start(self) -> None:
        """Загрузить голос Piper."""
        if self._voice is not None:
            return
        logger.info("Загружаю голос Piper: %s", self._config.voice)
        self._voice = await self._worker.run(self._load)
        logger.info("Piper готов, частота %d Гц", self._sample_rate)

    def _load(self) -> Any:
        """Синхронная загрузка голоса — выполняется в пуле потоков."""
        from piper import PiperVoice

        path = self.model_path
        if not path.is_file():
            raise FileNotFoundError(
                f"Голос Piper не найден: {path}. "
                f"Скачай командой: python -m jarvis --download-voice {self._config.voice}"
            )
        voice = PiperVoice.load(str(path))
        # Частота задаётся моделью; конфиг тут не указ.
        rate = getattr(getattr(voice, "config", None), "sample_rate", None)
        if rate:
            self._sample_rate = int(rate)
        return voice

    async def stop(self) -> None:
        """Освободить голос."""
        self._voice = None

    async def synthesize(self, text: str) -> Speech:
        """Синтезировать речь, не блокируя event loop."""
        if self._voice is None:
            raise RuntimeError("Piper не загружен — вызови start()")
        if not text.strip():
            return Speech(audio=b"", sample_rate=self._sample_rate, text=text)
        audio, rate = await self._worker.run(self._synthesize, text)
        return Speech(audio=audio, sample_rate=rate, text=text)

    def _synthesize(self, text: str) -> tuple[bytes, int]:
        """Синхронный синтез — выполняется в пуле потоков."""
        from piper import SynthesisConfig

        settings = SynthesisConfig(length_scale=self._config.length_scale)
        chunks = list(self._voice.synthesize(text, syn_config=settings))
        if not chunks:
            return b"", self._sample_rate
        audio = b"".join(chunk.audio_int16_bytes for chunk in chunks)
        return audio, int(chunks[0].sample_rate)

    async def say(self, text: str) -> None:
        """Синтезировать и отправить в аудиовыход."""
        speech = await self.synthesize(text)
        if not speech.empty:
            await self._sink.play(speech.audio, sample_rate=speech.sample_rate)
