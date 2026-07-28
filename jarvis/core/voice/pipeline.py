"""Голосовой конвейер: микрофон -> VAD -> wake word -> STT -> роутер -> TTS.

Каждое звено — отдельный протокол, поэтому заменяется поштучно: поставить
Silero вместо пропускающего VAD или другой STT можно, не трогая остальное.

Текстовая команда (``--say``, Telegram, веб) идёт по тому же пути начиная с
роутера — общий код, одинаковое поведение.
"""

from __future__ import annotations

import asyncio
import logging
import time

from jarvis.core.audio import VAD, AudioFrame, AudioSink, AudioSource, WakeWord
from jarvis.core.config import AudioConfig
from jarvis.core.contracts import (
    AssistantReplied,
    ToolResult,
    Utterance,
    VoiceCommandRecognized,
    WakeWordDetected,
)
from jarvis.core.bus import EventBus
from jarvis.core.router import Dispatcher
from jarvis.core.stt import STT
from jarvis.core.tts import TTS

logger = logging.getLogger(__name__)

#: Сколько кадров тишины подряд считать концом фразы.
_SILENCE_FRAMES = 25
#: Предохранитель от бесконечной записи, кадров.
_MAX_FRAMES = 600


class VoicePipeline:
    """Связывает аудиотракт, распознавание, маршрутизацию и синтез."""

    def __init__(
        self,
        *,
        source: AudioSource,
        sink: AudioSink,
        vad: VAD,
        wake_word: WakeWord,
        stt: STT,
        tts: TTS,
        dispatcher: Dispatcher,
        events: EventBus,
        config: AudioConfig,
    ) -> None:
        self._source = source
        self._sink = sink
        self._vad = vad
        self._wake_word = wake_word
        self._stt = stt
        self._tts = tts
        self._dispatcher = dispatcher
        self._events = events
        self._config = config
        self._task: asyncio.Task[None] | None = None

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "voice"

    async def start(self) -> None:
        """Запустить цикл прослушивания в фоне."""
        if self._task is None:
            self._task = asyncio.create_task(self._listen(), name="voice-pipeline")
            logger.debug("Голосовой конвейер запущен")

    async def stop(self) -> None:
        """Остановить цикл прослушивания."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # --- общий путь для голоса и текста ------------------------------------

    async def handle(self, utterance: Utterance) -> ToolResult:
        """Провести реплику через роутер и озвучить ответ."""
        result = await self._dispatcher.handle(utterance)
        reply = result.speech or self._describe(result)
        if reply:
            await self._tts.say(reply)
            self._events.emit(
                AssistantReplied(source="voice", text=reply, spoken=self._tts.ready)
            )
        return result

    @staticmethod
    def _describe(result: ToolResult) -> str:
        """Собрать реплику, если инструмент не предложил свою."""
        if not result.ok:
            return result.error or "Не получилось выполнить команду."
        if result.value is None:
            return "Готово."
        return str(result.value)

    # --- цикл прослушивания ------------------------------------------------

    async def _listen(self) -> None:
        """Слушать микрофон и обрабатывать распознанные фразы."""
        buffer = bytearray()
        silence = 0
        armed = False

        try:
            async for frame in self._source.frames():
                if not armed:
                    if not self._wake_word.detect(frame):
                        continue
                    armed = True
                    self._wake_word.reset()
                    self._events.emit(
                        WakeWordDetected(source="voice", phrase=self._wake_word.phrase)
                    )

                if self._vad.is_speech(frame):
                    buffer.extend(frame.data)
                    silence = 0
                elif buffer:
                    silence += 1

                too_long = len(buffer) >= _MAX_FRAMES * len(frame.data or b"\0")
                if buffer and (silence >= _SILENCE_FRAMES or too_long):
                    await self._process(bytes(buffer))
                    buffer.clear()
                    silence = 0
                    armed = False
                    self._vad.reset()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Голосовой конвейер остановлен из-за ошибки")

    async def _process(self, audio: bytes) -> None:
        """Распознать накопленный фрагмент и обработать реплику."""
        started = time.perf_counter()
        transcript = await self._stt.transcribe(audio, sample_rate=self._config.sample_rate)
        if transcript.empty:
            logger.debug("Распознавание не дало текста")
            return

        logger.info("Распознано за %.2f с: %r", time.perf_counter() - started, transcript.text)
        self._events.emit(
            VoiceCommandRecognized(
                source="voice",
                text=transcript.text,
                confidence=transcript.confidence,
                language=transcript.language,
            )
        )
        await self.handle(Utterance(text=transcript.text, source="voice"))
