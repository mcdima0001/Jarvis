"""Адаптер Piper TTS — локальный синтез с минимальной задержкой.

Piper синхронный: и загрузка голоса, и синтез уходят в `BlockingWorker`, иначе
на время генерации встанет весь event loop.

Голос выбирается под язык реплики: русский голос английский текст внятно не
прочтёт, и наоборот. Голоса грузятся лениво и кешируются — платить памятью за
английский, если на нём ни разу не заговорили, незачем.

API Piper: `PiperVoice.load(path)` и `voice.synthesize(text, syn_config)`,
возвращающий поток `AudioChunk` с полями `audio_int16_bytes` и `sample_rate`.
Частота дискретизации берётся из самого голоса, а не из конфига.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from jarvis.core.audio import AudioSink
from jarvis.core.config import TTSConfig
from jarvis.core.runtime import BlockingWorker

from .normalize import normalize_for_speech
from .protocol import Speech

logger = logging.getLogger(__name__)


class PiperTTS:
    """Синтез речи через Piper с отдельным голосом на каждый язык."""

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
        self._voices: dict[str, Any] = {}
        self._rates: dict[str, int] = {}

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "tts"

    @property
    def ready(self) -> bool:
        """Загружен ли хотя бы один голос."""
        return bool(self._voices)

    def model_path(self, voice: str) -> Path:
        """Путь к файлу голоса."""
        return self._config.models_dir / f"{voice}.onnx"

    async def start(self) -> None:
        """Загрузить голос языка по умолчанию; остальные — по требованию."""
        language, voice = self._config.voice_for(self._config.default_language)
        if not voice:
            raise FileNotFoundError(
                "Не задан ни один голос. Пропиши tts.voices в config.yaml, "
                "список: python -m jarvis --download-voice"
            )
        await self._ensure(language)

    async def stop(self) -> None:
        """Освободить голоса."""
        self._voices.clear()
        self._rates.clear()

    async def _ensure(self, language: str) -> str:
        """Загрузить голос языка, если он ещё не в памяти. Вернуть код языка."""
        code, voice = self._config.voice_for(language)
        if code in self._voices:
            return code
        if not voice:
            raise FileNotFoundError(f"Для языка {language!r} не задан голос в tts.voices")

        logger.info("Загружаю голос Piper: %s (%s)", voice, code)
        loaded, rate = await self._worker.run(self._load, voice)
        self._voices[code] = loaded
        self._rates[code] = rate
        logger.info("Голос %s готов, частота %d Гц", voice, rate)
        return code

    def _load(self, voice: str) -> tuple[Any, int]:
        """Синхронная загрузка голоса — выполняется в пуле потоков."""
        from piper import PiperVoice

        path = self.model_path(voice)
        if not path.is_file():
            raise FileNotFoundError(
                f"Голос Piper не найден: {path}. "
                f"Скачай командой: python -m jarvis --download-voice {voice}"
            )
        loaded = PiperVoice.load(str(path))
        rate = getattr(getattr(loaded, "config", None), "sample_rate", None)
        return loaded, int(rate or self._config.sample_rate)

    async def synthesize(self, text: str, *, language: str | None = None) -> Speech:
        """Синтезировать речь, не блокируя event loop."""
        if not text.strip():
            return Speech(audio=b"", sample_rate=self._config.sample_rate, text=text)

        code = await self._ensure(language or self._config.default_language)

        # Чужой алфавит голос читает как кашу, поэтому готовим текст здесь,
        # а не в каждом скилле: латиница попадает в речь ещё и подстановками.
        spoken = normalize_for_speech(text, self._config.pronounce, language=code)
        if spoken != text:
            logger.debug("Текст для синтеза (%s): %r -> %r", code, text, spoken)

        audio, rate = await self._worker.run(self._synthesize, code, spoken)
        return Speech(audio=audio, sample_rate=rate, text=text, language=code)

    def _synthesize(self, language: str, text: str) -> tuple[bytes, int]:
        """Синхронный синтез — выполняется в пуле потоков."""
        from piper import SynthesisConfig

        settings = SynthesisConfig(length_scale=self._config.length_scale)
        chunks = list(self._voices[language].synthesize(text, syn_config=settings))
        if not chunks:
            return b"", self._rates.get(language, self._config.sample_rate)
        audio = b"".join(chunk.audio_int16_bytes for chunk in chunks)
        return audio, int(chunks[0].sample_rate)

    async def say(self, text: str, *, language: str | None = None) -> None:
        """Синтезировать и отправить в аудиовыход."""
        speech = await self.synthesize(text, language=language)
        if not speech.empty:
            await self._sink.play(speech.audio, sample_rate=speech.sample_rate)
