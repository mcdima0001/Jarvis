"""Голосовой конвейер: микрофон -> VAD -> STT -> имя -> роутер -> TTS.

Каждое звено — отдельный протокол, поэтому заменяется поштучно.

Два практических решения, которые видны только на живой речи:

* **Распознавание не блокирует прослушивание.** Whisper работает секунды;
  если ждать его в цикле чтения кадров, следующая фраза потеряется. Поэтому
  фрагменты уходят в очередь, а разбирает их отдельная задача.
* **Имя ловится двумя способами сразу, и это не дубль.** По тексту — дёшево:
  фраза всё равно расшифровывается, остаётся посмотреть, начата ли она с имени
  (сравнение нечёткое, Whisper пишет то «Джарвис», то «Джарвес», то «Жарвис»).
  По звуку — своей моделью, если она обучена: она срабатывает **до**
  распознавания, и только так можно успеть приглушить музыку раньше, чем
  прозвучит команда. Модель промолчала — остаётся текстовый гейт, и наоборот.

Текстовая команда (``--say``, Telegram, веб) идёт по тому же пути начиная с
роутера — общий код, одинаковое поведение.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import time

from jarvis.core.audio import (
    VAD,
    AlwaysActiveWakeWord,
    AudioSink,
    AudioSource,
    WakeWord,
    load_sound,
)
from jarvis.core.bus import EventBus
from jarvis.core.config import AudioConfig
from jarvis.core.contracts import (
    AssistantReplied,
    AssistantSpeaking,
    ToolResult,
    Utterance,
    VoiceCommandRecognized,
    WakeWordDetected,
)
from jarvis.core.persona import DONE, FAILED, LISTENING, Persona
from jarvis.core.router import Dispatcher
from jarvis.core.state import DEAF, Modes, wakes_up
from jarvis.core.stt import STT
from jarvis.core.tts import TTS

logger = logging.getLogger(__name__)

#: Сколько фрагментов держать в очереди на распознавание, если в конфиге
#: не сказано иное. Больше двух держать смысла мало: распознавание идёт
#: примерно в реальном времени, и очередь означает, что команда выполнится
#: с опозданием на её длину.
_PENDING_LIMIT = 2


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
        persona: Persona | None = None,
        modes: Modes | None = None,
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
        # Без явной персоны берётся стандартная: конвейер должен собираться и
        # в тесте, где характер ассистента не при чём.
        self._persona = persona or Persona()
        #: Режимы. Конвейеру интересен один — «не слушаю»: он решается тут и
        #: только тут, потому что гейт обязан стоять **до** роутера. Проверять
        #: его в инструменте было бы поздно: реплика уже уехала бы в модель.
        self._modes = modes if modes is not None else Modes()

        # Вместе со звуком храним момент, когда он прозвучал: окно ответа
        # должно отсчитываться от речи, а не от того, когда до неё дошли руки.
        self._pending: asyncio.Queue[tuple[bytes, float]] = asyncio.Queue(
            maxsize=max(1, config.pending_limit or _PENDING_LIMIT)
        )
        self._tasks: list[asyncio.Task[None]] = []
        #: Что произнесли последним. Нужно `--say`: реплику выбирает персона, и
        #: угадать её со стороны нельзя — напечатали бы не то, что сказали.
        self.last_reply = ""
        #: Не озвучивать вовсе. Пропустить сервис синтеза недостаточно: голоса
        #: грузятся лениво, при первом обращении, и `--no-voice` всё равно
        #: поднимал модель — просто позже и молча, уже после ответа.
        self.silent = False
        self._follow_up_until = 0.0
        #: Слушать ли имя по звуку. Признак — не режим из конфига, а то,
        #: поднялась ли настоящая модель: заглушка в этом слоте отвечает «да»
        #: на любой кадр, и спрашивать её означало бы срабатывать всегда.
        self._acoustic = not isinstance(wake_word, AlwaysActiveWakeWord)
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
        if self._acoustic:
            logger.info("Слушаю. Имя «%s» ловлю моделью, по звуку", phrase)
        elif self._config.wake_word.mode in ("text", "acoustic"):
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

    async def announce(self, situation: str, *, language: str | None = None) -> None:
        """Произнести служебную реплику: приветствие, прощание.

        Вынесено из ``start``/``stop`` намеренно. Здороваться уместно в начале
        живого сеанса, а не при каждом подъёме сервиса: иначе ``--check`` и
        одиночная команда ``--say`` тоже здоровались бы и прощались, тратя на
        это синтез и секунды.
        """
        await self._say(self._persona.line(situation, language), language=language)

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
        # Вариант выбирает персона, а не скилл: она помнит, что уже говорила, и
        # у каждой команды своя память — «пауза» не вытесняет «включаю».
        options = result.speech_options(utterance.language)
        reply = self._persona.choose(
            result.tool or "tool", options, utterance.language
        ) or self._describe(result, utterance.language)
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
        if not text:
            return
        # Что именно сказал ассистент, по логу иначе не восстановить: в нём
        # видно команду и её результат, а произнесённой фразы — нет. А разбирать
        # приходится как раз расхождение между ними.
        # `tone` — пометка для консоли: реплики разговора там ищут глазами
        # первыми. Помечаем полем записи, а не подсветкой по тексту сообщения:
        # угадывание рассыпалось бы при первой правке формулировки.
        logger.info("Отвечаю: %s", text, extra={"tone": "said"})
        self.last_reply = text
        if self.silent:
            # Голос выключен целиком: реплика уже в логе, а трогать синтез
            # нельзя — он загрузит модель при первом же обращении.
            self._events.emit(AssistantReplied(source="voice", text=text, spoken=False))
            return
        self._speaking = True
        spoken = True
        # Сообщаем **до** первого слова: музыку надо успеть приглушить, иначе
        # ответа не слышно. Синтез занимает доли секунды — их и хватает.
        self._events.emit(AssistantSpeaking(source="voice", text=text))
        try:
            await self._tts.say(text, language=language)
        except Exception as exc:  # noqa: BLE001 — немой ответ лучше упавшего цикла
            # Сбойный голос одного языка не должен обрывать разговор: реплику
            # хотя бы видно в логе, и ассистент продолжает слушать.
            spoken = False
            logger.error("Не удалось озвучить реплику (%s): %s", type(exc).__name__, exc)
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

    def _describe(self, result: ToolResult, language: str | None = None) -> str:
        """Собрать реплику, если инструмент не предложил свою.

        Своё объяснение ошибки важнее вежливого отказа: «Не нашёл такой
        программы» полезнее, чем «Не вышло, сэр».
        """
        if not result.ok:
            return result.error or self._persona.line(FAILED, language)
        if result.value is None:
            return self._persona.line(DONE, language)
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

                if self._acoustic and self._wake_word.detect(frame):
                    self._on_name_heard()

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

    def _on_name_heard(self) -> None:
        """Модель активации услышала имя — ещё до всякого распознавания.

        Ради этого момента она и нужна: событие уходит в шину сразу, и музыку
        успевают приглушить **до** того, как команда прозвучит. Текстовый гейт
        такого не может в принципе — он узнаёт имя после Whisper, когда фраза
        уже записана вместе с фоном.

        Дальше всё идёт по накатанному: открывается то же окно, что и после
        голого «Джарвис», а имя из расшифровки снимет `_strip_wake`.
        """
        self._wake_word.reset()
        # В режиме «не слушаю» имя ничего не открывает и никого не будит.
        # Событие отсюда приглушает музыку, и без этой проверки каждое
        # случайное «Джарвис» дёргало бы громкость у молчащего ассистента.
        if self._modes.active(DEAF):
            logger.debug("Услышал имя, но сейчас не слушаю")
            return
        self._follow_up_until = time.time() + self._config.wake_word.follow_up_s
        logger.info(
            "Услышал имя (%.2f) — жду команду %.0f с",
            getattr(self._wake_word, "score", 1.0),
            self._config.wake_word.follow_up_s,
        )
        self._events.emit(
            WakeWordDetected(
                source="voice",
                phrase=self._wake_word.phrase,
                score=float(getattr(self._wake_word, "score", 1.0)),
            )
        )

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
            # Чаще всего это не «медленный компьютер», а посторонняя речь:
            # видео в колонках или разговор рядом. Whisper распознаёт примерно
            # в реальном времени, поэтому непрерывный фон забивает очередь.
            logger.warning(
                "Не успеваю распознавать — фрагмент %.1f с отброшен. "
                "Если повторяется: посторонняя речь в микрофоне либо тяжёлая "
                "модель (stt.model)",
                len(audio) / 2 / self._config.sample_rate,
            )

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
            extra={"tone": "heard"},
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
            await self._say(self._persona.line(LISTENING, language), language=language)
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

    def _deaf_gate(self, text: str) -> str | None:
        """Что пропускать, пока включён режим «не слушаю».

        Ровно одно: обращение по имени плюс фраза пробуждения. Всё остальное
        останавливается **здесь**, до роутера, — то есть не стоит ни секунды
        ожидания, ни токена.

        Сама фраза при этом идёт дальше как обычная команда: её узнаёт резолвер
        фраз и зовёт `core.as_usual`. Отдельного пути для пробуждения нет
        намеренно — список фраз тут и у инструмента один и тот же
        (`WAKE_PHRASES`), поэтому разъехаться им негде.
        """
        called, command = self._strip_wake(text)
        # Имя обязательно: гейт должен быть строже обычного, а не мягче.
        # Без него случайное «слушай» в разговоре рядом будило бы ассистента.
        if self._config.wake_word.mode in ("text", "acoustic") and not called:
            logger.debug("Не слушаю: реплика без имени пропущена")
            return None
        if not wakes_up(command):
            logger.debug("Не слушаю: %r не похоже на просьбу вернуться", command)
            return None
        logger.info("Просыпаюсь по фразе %r", command)
        return command

    def _extract_command(self, text: str, *, spoken_at: float | None = None) -> str | None:
        """Решить, обращались ли к ассистенту, и вернуть команду.

        :param spoken_at: когда фраза прозвучала; по умолчанию — сейчас
            (текстовый ввод приходит без задержки на распознавание).
        :return: текст команды; пустая строка, если позвали только по имени;
            ``None``, если обращения не было и окно ответа закрыто.
        """
        if self._modes.active(DEAF):
            return self._deaf_gate(text)

        called, command = self._strip_wake(text)

        if self._config.wake_word.mode not in ("text", "acoustic"):
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
