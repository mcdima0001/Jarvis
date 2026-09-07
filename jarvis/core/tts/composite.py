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
import time
from typing import Any

#: Сколько не трогать основной голос после отказа. Реплики звучат десятки раз
#: за вечер, и ждать таймаут облака на каждой значит превратить обрыв связи в
#: «ассистент задумывается перед каждым словом».
RETRY_AFTER_S = 60.0

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
        #: До какого момента не трогать основной голос после отказа.
        self._blocked_until = 0.0
        self._spare_language = self._find_spare_language()

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "tts"

    @property
    def ready(self) -> bool:
        """Загружен ли хотя бы один голос."""
        return bool(self._loaded)

    def _find_spare_language(self) -> str:
        """На каком языке говорит запасной голос.

        Ищется среди `tts.voices`: запасной обязан быть там же, иначе он не
        загрузится при старте и в нужный момент окажется таким же недоступным,
        как основной. Не нашёлся — предупреждаем сразу, при сборке, а не в
        момент отказа, когда сказать об этом будет уже нечем.
        """
        if not self._config.fallback:
            return ""
        wanted = parse_voice(self._config.fallback, default_engine=self._config.engine)
        for language in self._config.voices:
            if parse_voice(
                self._config.voices[language], default_engine=self._config.engine
            ) == wanted:
                return language
        logger.warning(
            "Запасной голос %r не указан ни для одного языка в tts.voices — "
            "он не загрузится при старте, и откат работать не будет",
            self._config.fallback,
        )
        return self._config.default_language

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
                api_key=self._config.api_key,
                model=self._config.model,
                timeout=self._config.timeout,
            )
            self._backends[engine] = backend
        return backend

    async def start(self) -> None:
        """Загрузить все голоса из конфига.

        Раньше грелся только голос языка по умолчанию, а остальные загружались
        при первом обращении — то есть посреди разговора. С лёгким движком это
        незаметно, но тяжёлый (XTTS на процессоре) так подвешивает первую же
        реплику на своём языке на минуты, и со стороны это выглядит поломкой.
        Лучше заплатить это время один раз при запуске, где оно видно в логе.

        Голос языка по умолчанию обязателен, остальные — нет: без английской
        модели разумнее работать по-русски, чем не запуститься совсем.
        """
        code, engine, voice = self.resolve(self._config.default_language)
        if not voice:
            raise FileNotFoundError(
                "Не задан ни один голос. Пропиши tts.voices в config.yaml, "
                "список: python -m jarvis --download-voice"
            )
        try:
            await self._ensure(code, engine, voice)
        except Exception as exc:  # noqa: BLE001 — облако, сеть, кончился тариф
            # С облачным основным голосом это не редкость: нет сети — нет и
            # голоса. Падать тут нельзя, пока есть чем говорить: ассистент без
            # синтеза работает хуже, а не запустившийся не работает вовсе.
            if self._spare(engine, voice) is None:
                raise
            self._blocked_until = time.monotonic() + RETRY_AFTER_S
            logger.warning(
                "Основной голос %s:%s не поднялся (%s: %s) — говорю запасным %s",
                engine,
                voice,
                type(exc).__name__,
                exc,
                self._config.fallback,
            )

        for language in self._config.voices:
            other_code, other_engine, other_voice = self.resolve(language)
            try:
                await self._ensure(other_code, other_engine, other_voice)
            except Exception as exc:  # noqa: BLE001 — один голос не рушит запуск
                logger.warning(
                    "Голос %s:%s для языка %s не загрузился (%s): "
                    "на этом языке синтеза не будет",
                    other_engine,
                    other_voice,
                    other_code,
                    exc,
                )

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
        spare = self._spare(engine, voice)
        if spare is not None and time.monotonic() < self._blocked_until:
            # Основной голос недавно отказал — не ждём его таймаут на каждой
            # реплике, сразу говорим запасным.
            return await self._speak(text, *spare)

        try:
            return await self._speak(text, code, engine, voice)
        except Exception as exc:  # noqa: BLE001 — облако, сеть, кончился тариф
            if spare is None:
                raise
            self._blocked_until = time.monotonic() + RETRY_AFTER_S
            logger.warning(
                "Голос %s:%s не отозвался (%s: %s) — перехожу на запасной %s:%s на %.0f с",
                engine,
                voice,
                type(exc).__name__,
                exc,
                spare[1],
                spare[2],
                RETRY_AFTER_S,
            )
            return await self._speak(text, *spare)

    async def _speak(self, text: str, code: str, engine: str, voice: str) -> Speech:
        """Синтезировать конкретным голосом.

        Язык здесь — язык **голоса**, а не вопроса, и от него зависит подготовка
        текста: английский голос кириллицу читает как кашу, поэтому
        `normalize_for_speech` переводит её латиницей. На запасном голосе это и
        спасает: русская реплика звучит с акцентом, но разборчиво.
        """
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

    def _spare(self, engine: str, voice: str) -> tuple[str, str, str] | None:
        """Запасной голос, если он задан и не совпадает с основным.

        Язык берётся из `tts.voices`: там этот голос уже прописан под свой язык,
        и гадать не нужно. Не прописан — откат не работает, о чём сказано при
        сборке: молчащий запасной хуже отсутствующего, потому что о нём думают,
        что он есть.
        """
        if not self._config.fallback:
            return None
        spare_engine, spare_voice = parse_voice(
            self._config.fallback, default_engine=self._config.engine
        )
        if (spare_engine, spare_voice) == (engine, voice):
            return None
        return self._spare_language, spare_engine, spare_voice

    async def say(self, text: str, *, language: str | None = None) -> None:
        """Синтезировать и отправить в аудиовыход."""
        speech = await self.synthesize(text, language=language)
        if not speech.empty:
            await self._sink.play(speech.audio, sample_rate=speech.sample_rate)
