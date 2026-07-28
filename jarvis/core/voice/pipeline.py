"""Голосовой конвейер: микрофон -> VAD -> STT -> имя -> роутер -> TTS.

Каждое звено — отдельный протокол, поэтому заменяется поштучно.

Два практических решения, которые видны только на живой речи:

* **Распознавание не блокирует прослушивание.** Whisper работает секунды;
  если ждать его в цикле чтения кадров, следующая фраза потеряется. Поэтому
  фрагменты уходят в очередь, а разбирает их отдельная задача.
* **Активация проверяется по тексту, а не по звуку.** Распознавание идёт
  локально и бесплатно, так что дешевле расшифровать фразу и посмотреть, начата
  ли она с имени, чем держать отдельную модель wake word. Сравнение нечёткое:
  Whisper пишет то «Джарвис», то «Джарвес», то «Жарвис».

Текстовая команда (``--say``, Telegram, веб) идёт по тому же пути начиная с
роутера — общий код, одинаковое поведение.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import time

from jarvis.core.audio import VAD, AudioSink, AudioSource, WakeWord, load_sound
from jarvis.core.bus import EventBus
from jarvis.core.config import AudioConfig
from jarvis.core.contracts import (
    AssistantReplied,
    ToolResult,
    Utterance,
    VoiceCommandRecognized,
    WakeWordDetected,
)
from jarvis.core.router import Dispatcher
from jarvis.core.stt import STT
from jarvis.core.tts import TTS

logger = logging.getLogger(__name__)

#: Сколько фрагментов держать в очереди на распознавание.
_PENDING_LIMIT = 2
#: Ответ на голое обращение по имени.
_LISTENING_REPLY = {"ru": "Слушаю.", "en": "I'm listening."}


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

        # Вместе со звуком храним момент, когда он прозвучал: окно ответа
        # должно отсчитываться от речи, а не от того, когда до неё дошли руки.
        self._pending: asyncio.Queue[tuple[bytes, float]] = asyncio.Queue(
            maxsize=_PENDING_LIMIT
        )
        self._tasks: list[asyncio.Task[None]] = []
        self._follow_up_until = 0.0
        self._speaking = False
        self._mute_until = 0.0
        #: Отклик на распознанную команду: PCM и частота, либо None.
        self._activation: tuple[bytes, int] | None = None
        #: Ссылку держим, чтобы задачу не собрал сборщик мусора на полпути.
        self._sound_task: asyncio.Task[None] | None = None

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "voice"

    async def start(self) -> None:
        """Запустить прослушивание и разбор."""
        if self._tasks:
            return
        # Звук читаем один раз при старте: раскодировать его на каждой команде
        # значило бы добавлять задержку ровно там, где нужен мгновенный отклик.
        if self._config.activation_sound is not None:
            self._activation = await asyncio.to_thread(
                load_sound, self._config.activation_sound
            )
            if self._activation is not None:
                seconds = len(self._activation[0]) / 2 / self._activation[1]
                logger.info(
                    "Звук активации готов: %s (%.2f с)",
                    self._config.activation_sound.name,
                    seconds,
                )

        self._tasks = [
            asyncio.create_task(self._listen(), name="voice-listen"),
            asyncio.create_task(self._consume(), name="voice-recognize"),
        ]
        phrase = self._config.wake_word.phrase
        if self._config.wake_word.mode == "text":
            logger.info("Слушаю. Обращение по имени: «%s»", phrase)
        else:
            logger.info("Слушаю. Реагирую на любую распознанную фразу")

    async def stop(self) -> None:
        """Остановить прослушивание и разбор."""
        if self._sound_task is not None:
            self._sound_task.cancel()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    # --- общий путь для голоса и текста ------------------------------------

    async def handle(self, utterance: Utterance) -> ToolResult:
        """Провести реплику через роутер и озвучить ответ.

        Обращение по имени вырезается независимо от источника: и «Джарвис,
        включи свет» из микрофона, и то же самое текстом должны попасть в
        роутер как «включи свет».
        """
        _, command = self._strip_wake(utterance.text)
        if command != utterance.text:
            utterance = Utterance(
                text=command,
                language=utterance.language,
                confidence=utterance.confidence,
                source=utterance.source,
            )
        result = await self._dispatcher.handle(utterance)
        reply = result.speech_for(utterance.language) or self._describe(
            result, utterance.language
        )
        if reply:
            await self._say(reply, language=utterance.language)
        return result

    async def _say(self, text: str, *, language: str | None = None) -> None:
        """Озвучить реплику, заглушив на это время микрофон.

        Без этого получается акустическая петля: колонки произносят ответ,
        микрофон его слышит, Whisper расшифровывает — и ассистент разбирает
        собственную реплику как команду. В окне ответа, где имя не требуется,
        он её ещё и выполнит.
        """
        self._speaking = True
        spoken = True
        try:
            await self._tts.say(text, language=language)
        except Exception as exc:  # noqa: BLE001 — немой ответ лучше упавшего цикла
            # Сбойный голос одного языка не должен обрывать разговор: реплику
            # хотя бы видно в логе, и ассистент продолжает слушать.
            spoken = False
            logger.error("Не удалось озвучить реплику (%s): %s", type(exc).__name__, exc)
            logger.info("Ответ (без голоса): %s", text)
        finally:
            # Колонки ещё звучат, плюс реверберация комнаты.
            self._mute_until = time.time() + self._config.echo_tail_ms / 1000
            self._speaking = False

        self._events.emit(
            AssistantReplied(source="voice", text=text, spoken=spoken and self._tts.ready)
        )

    async def _play_activation(self) -> None:
        """Отозваться коротким звуком на распознанную команду.

        Микрофон на это время глушится так же, как на собственную речь: иначе
        отклик попадёт в следующий фрагмент и Whisper начнёт искать в нём слова.
        """
        if self._activation is None:
            return
        audio, rate = self._activation
        self._speaking = True
        try:
            await self._sink.play(audio, sample_rate=rate)
        except Exception as exc:  # noqa: BLE001 — звук не повод рвать команду
            logger.warning("Не удалось воспроизвести отклик: %s", exc)
        finally:
            self._mute_until = time.time() + self._config.echo_tail_ms / 1000
            self._speaking = False

    @property
    def _muted(self) -> bool:
        """Глушить ли сейчас микрофон (говорим сами или ещё звучит хвост)."""
        return self._speaking or time.time() < self._mute_until

    @staticmethod
    def _describe(result: ToolResult, language: str | None = None) -> str:
        """Собрать реплику, если инструмент не предложил свою."""
        english = (language or "ru").startswith("en")
        if not result.ok:
            if result.error:
                return result.error
            return "Couldn't do that." if english else "Не получилось выполнить команду."
        if result.value is None:
            return "Done." if english else "Готово."
        return str(result.value)

    # --- захват ------------------------------------------------------------

    async def _listen(self) -> None:
        """Читать кадры и собирать из них фразы."""
        buffer = bytearray()
        silence = 0
        speaking = False

        try:
            async for frame in self._source.frames():
                # Пока говорим сами — кадры читаем, но выбрасываем: иначе
                # очередь захвата забьётся собственной речью.
                if self._muted:
                    if speaking:
                        logger.debug("Свою речь не слушаю, накопленное отбрасываю")
                    buffer.clear()
                    silence = 0
                    speaking = False
                    self._vad.reset()
                    continue

                if self._vad.is_speech(frame):
                    if not speaking:
                        logger.debug("Начало речи")
                    buffer.extend(frame.data)
                    silence = 0
                    speaking = True
                    continue

                if not speaking:
                    continue

                # Немного тишины оставляем в конце: Whisper лучше слышит границу.
                buffer.extend(frame.data)
                silence += 1

                too_long = len(buffer) >= self._config.max_utterance_bytes
                if silence >= self._config.silence_frames or too_long:
                    if too_long:
                        logger.debug("Фраза достигла предела длины, отправляю как есть")
                    self._submit(bytes(buffer))
                    buffer.clear()
                    silence = 0
                    speaking = False
                    self._vad.reset()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Цикл прослушивания остановлен из-за ошибки")

    def _submit(self, audio: bytes) -> None:
        """Отправить фрагмент на распознавание, не блокируя захват.

        Вместе с фрагментом запоминается момент, когда он **начал** звучать.
        Без этого окно ответа проверялось бы по времени разбора, а между речью
        и разбором лежит распознавание — несколько секунд. Пока Whisper думал,
        окно успевало закрыться, и ответ на «Слушаю» терялся.
        """
        if len(audio) < self._config.min_utterance_bytes:
            logger.debug("Фрагмент слишком короткий (%d байт), пропускаю", len(audio))
            return
        spoken_at = time.time() - len(audio) / 2 / self._config.sample_rate
        try:
            self._pending.put_nowait((audio, spoken_at))
        except asyncio.QueueFull:
            logger.warning("Не успеваю распознавать — фрагмент отброшен")

    # --- распознавание и разбор --------------------------------------------

    async def _consume(self) -> None:
        """Разбирать накопленные фрагменты по одному."""
        while True:
            audio, spoken_at = await self._pending.get()
            try:
                await self._process(audio, spoken_at)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Ошибка при разборе фрагмента")

    async def _process(self, audio: bytes, spoken_at: float) -> None:
        """Распознать фрагмент и обработать реплику.

        :param spoken_at: когда фраза прозвучала. Именно по этому времени
            проверяется окно ответа: распознавание идёт секунды, и сверяться
            с часами после него — значит закрывать окно раньше времени.
        """
        seconds = len(audio) / 2 / self._config.sample_rate
        started = time.perf_counter()
        transcript = await self._stt.transcribe(audio, sample_rate=self._config.sample_rate)
        if transcript.empty:
            logger.debug("Фрагмент %.1f с не дал текста", seconds)
            return

        logger.info(
            "Распознано (%.1f с речи за %.1f с): %r",
            seconds,
            time.perf_counter() - started,
            transcript.text,
        )

        command = self._extract_command(transcript.text, spoken_at=spoken_at)
        if command is None:
            logger.debug("Обращения по имени нет — пропускаю")
            return

        language = transcript.language or "ru"

        if not command:
            # Позвали по имени и замолчали: отвечаем и ждём команду без имени.
            self._events.emit(
                WakeWordDetected(source="voice", phrase=self._config.wake_word.phrase)
            )
            reply = _LISTENING_REPLY.get(language.split("-")[0], _LISTENING_REPLY["ru"])
            await self._say(reply, language=language)
            # Окно открывается только теперь, когда «Слушаю» отзвучало и стих
            # хвост. Если отсчитывать от распознавания, треть времени съедает
            # собственная реплика, и обещанные секунды оказываются короче.
            self._follow_up_until = self._mute_until + self._config.wake_word.follow_up_s
            logger.info(
                "Жду команду без имени %.0f с", self._config.wake_word.follow_up_s
            )
            return

        self._follow_up_until = 0.0
        # Отклик играет параллельно с выполнением, а не до него: он говорит
        # «услышал», и задерживать ради него саму команду незачем. Ответ всё
        # равно прозвучит после — динамик занят по очереди.
        self._sound_task = asyncio.create_task(self._play_activation())
        self._events.emit(
            VoiceCommandRecognized(
                source="voice",
                text=command,
                confidence=transcript.confidence,
                language=language,
            )
        )
        await self.handle(
            Utterance(text=command, language=language, source="voice")
        )

    def _strip_wake(self, text: str) -> tuple[bool, str]:
        """Отделить обращение по имени от самой команды.

        :return: пара «звали по имени» и текст команды без имени.
        """
        settings = self._config.wake_word
        cleaned = " ".join(text.split()).strip(" .,!?;:")
        words = cleaned.split()
        if not words:
            return False, ""

        first = words[0].lower().strip(" .,!?;:—-")
        remainder = " ".join(words[1:]).strip(" ,")

        if first in settings.aliases or first in settings.phrases:
            logger.debug("Имя распознано: %r", first)
            return True, remainder

        # Сравниваем с каждым написанием: «jarvis» и «джарвис» в разных
        # алфавитах, и похожесть между ними нулевая.
        ratio = max(
            (difflib.SequenceMatcher(None, first, phrase).ratio() for phrase in settings.phrases),
            default=0.0,
        )
        if ratio >= settings.similarity:
            logger.debug("Имя распознано: %r (похожесть %.2f)", first, ratio)
            return True, remainder

        # Почти совпало — скорее всего звали, но модель ослышалась.
        # Показываем на уровне INFO: иначе непонятно, почему ассистент молчит.
        if ratio >= 0.45:
            logger.info(
                "Похоже на обращение, но не уверен: %r ~ %r (%.2f). "
                "Добавь вариант в audio.wake_word.aliases, если повторяется",
                first,
                settings.phrase,
                ratio,
            )
        return False, cleaned

    def _extract_command(self, text: str, *, spoken_at: float | None = None) -> str | None:
        """Решить, обращались ли к ассистенту, и вернуть команду.

        :param spoken_at: когда фраза прозвучала; по умолчанию — сейчас
            (текстовый ввод приходит без задержки на распознавание).
        :return: текст команды; пустая строка, если позвали только по имени;
            ``None``, если обращения не было и окно ответа закрыто.
        """
        called, command = self._strip_wake(text)

        if self._config.wake_word.mode != "text":
            return command if called else text.strip()

        if called:
            return command

        moment = time.time() if spoken_at is None else spoken_at
        if moment < self._follow_up_until:
            logger.debug(
                "Окно ответа открыто ещё %.1f с — имя не требуется",
                self._follow_up_until - moment,
            )
            return command

        return None
