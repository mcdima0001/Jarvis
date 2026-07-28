"""Адаптер Piper TTS — локальный синтез с минимальной задержкой.

Как и Whisper, Piper синхронный: и загрузка голоса, и синтез уходят в
`BlockingWorker`, иначе на время генерации встанет весь event loop.

Воспроизведение делегируется `AudioSink`, чтобы синтез и вывод звука можно
было заменять по отдельности.
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

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "tts"

    @property
    def ready(self) -> bool:
        """Загружен ли голос."""
        return self._voice is not None

    async def start(self) -> None:
        """Загрузить голос Piper."""
        if self._voice is not None:
            return
        logger.info("Загружаю голос Piper: %s", self._config.voice)
        self._voice = await self._worker.run(self._load)
        logger.info("Piper готов")

    def _load(self) -> Any:
        """Синхронная загрузка голоса — выполняется в пуле потоков."""
        from piper import PiperVoice

        model_path = self._config.models_dir / f"{self._config.voice}.onnx"
        if not model_path.is_file():
            raise FileNotFoundError(
                f"Голос Piper не найден: {model_path}. "
                f"Скачай модель с https://huggingface.co/rhasspy/piper-voices"
            )
        return PiperVoice.load(str(model_path))

    async def stop(self) -> None:
        """Освободить голос."""
        self._voice = None

    async def synthesize(self, text: str) -> Speech:
        """Синтезировать речь, не блокируя event loop."""
        if self._voice is None:
            raise RuntimeError("Piper не загружен — вызови start()")
        if not text.strip():
            return Speech(audio=b"", sample_rate=self._config.sample_rate, text=text)
        audio = await self._worker.run(self._synthesize, text)
        return Speech(audio=audio, sample_rate=self._config.sample_rate, text=text)

    def _synthesize(self, text: str) -> bytes:
        """Синхронный синтез — выполняется в пуле потоков."""
        chunks: list[bytes] = []
        for chunk in self._voice.synthesize_stream_raw(
            text,
            length_scale=self._config.length_scale,
        ):
            chunks.append(chunk)
        return b"".join(chunks)

    async def say(self, text: str) -> None:
        """Синтезировать и отправить в аудиовыход."""
        speech = await self.synthesize(text)
        if not speech.empty:
            await self._sink.play(speech.audio, sample_rate=speech.sample_rate)
