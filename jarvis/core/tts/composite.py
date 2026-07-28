"""Синтез речи: свой движок и голос на каждый язык.

Голос выбирается под язык реплики — русский голос английский текст внятно не
прочтёт, и наоборот. Движки при этом тоже могут быть разными: у Kokoro сильные
британские голоса, но нет русского; у Silero живой русский, но он тянет torch;
Piper легче всех. Поэтому в конфиге пишется ``движок:голос``, и для каждого
языка выбор независимый.

Всё тяжёлое уходит в `BlockingWorker`: загрузка модели и синтез — CPU-bound
работа, которая иначе заморозила бы event loop целиком.
"""

from __future__ import annotations

import logging
from typing import Any

from jarvis.core.audio import AudioSink
from jarvis.core.config import TTSConfig
from jarvis.core.runtime import BlockingWorker

from .backends import SpeechBackend, build_backend, parse_voice
from .normalize import normalize_for_speech
from .protocol import Speech

logger = logging.getLogger(__name__)


class CompositeTTS:
    """Синтез с отдельным движком и голосом на каждый язык."""

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
        self._backends: dict[str, SpeechBackend] = {}
        self._loaded: set[tuple[str, str]] = set()

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "tts"

    @property
    def ready(self) -> bool:
        """Загружен ли хотя бы один голос."""
        return bool(self._loaded)

    def resolve(self, language: str | None) -> tuple[str, str, str]:
        """Подобрать язык, движок и голос.

        :return: тройка «язык», «движок», «голос».
        """
        code, spec = self._config.voice_for(language)
        engine, voice = parse_voice(spec, default_engine=self._config.engine)
        return code, engine, voice

    def _backend(self, engine: str) -> SpeechBackend:
        """Взять движок из кеша или создать."""
        backend = self._backends.get(engine)
        if backend is None:
            backend = build_backend(
                engine,
                self._config.models_dir,
                length_scale=self._config.length_scale,
                device=self._config.device,
            )
            self._backends[engine] = backend
        return backend

    async def start(self) -> None:
        """Загрузить голос языка по умолчанию; остальные — при первом обращении."""
        code, engine, voice = self.resolve(self._config.default_language)
        if not voice:
            raise FileNotFoundError(
                "Не задан ни один голос. Пропиши tts.voices в config.yaml, "
                "список: python -m jarvis --download-voice"
            )
        await self._ensure(code, engine, voice)

    async def stop(self) -> None:
        """Освободить модели."""
        self._backends.clear()
        self._loaded.clear()

    async def _ensure(self, language: str, engine: str, voice: str) -> None:
        """Подготовить голос, если он ещё не загружен."""
        key = (engine, voice)
        if key in self._loaded:
            return
        logger.info("Загружаю голос %s:%s для языка %s", engine, voice, language)
        await self._worker.run(self._backend(engine).prepare, voice, language)
        self._loaded.add(key)
        logger.info("Голос %s:%s готов", engine, voice)

    async def synthesize(self, text: str, *, language: str | None = None) -> Speech:
        """Синтезировать речь, не блокируя event loop."""
        if not text.strip():
            return Speech(audio=b"", sample_rate=self._config.sample_rate, text=text)

        code, engine, voice = self.resolve(language)
        await self._ensure(code, engine, voice)

        # Чужой алфавит движок читает как кашу, поэтому текст готовим здесь,
        # а не в каждом скилле: латиница попадает в речь ещё и подстановками.
        spoken = normalize_for_speech(text, self._config.pronounce, language=code)
        if spoken != text:
            logger.debug("Текст для синтеза (%s): %r -> %r", code, text, spoken)

        audio, rate = await self._worker.run(
            self._backend(engine).synthesize, spoken, voice, code
        )
        return Speech(audio=audio, sample_rate=rate, text=text, language=code)

    async def say(self, text: str, *, language: str | None = None) -> None:
        """Синтезировать и отправить в аудиовыход."""
        speech = await self.synthesize(text, language=language)
        if not speech.empty:
            await self._sink.play(speech.audio, sample_rate=speech.sample_rate)
