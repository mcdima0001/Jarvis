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
import math
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from jarvis.core.attention import Announcer
from jarvis.core.audio import (
    VAD,
    AlwaysActiveWakeWord,
    AudioSink,
    AudioSource,
    WakeWord,
    load_sound,
)
from jarvis.core.audio.protocol import InterruptibleSink
from jarvis.core.bus import EventBus
from jarvis.core.config import AudioConfig
from jarvis.core.contracts import (
    LIVE_SPEECH,
    AnnouncementRequested,
    AssistantReplied,
    AssistantSpeaking,
    CommandTyped,
    Event,
    ToolResult,
    Utterance,
    VoiceCommandRecognized,
    WakeDismissed,
    WakeWordDetected,
    dominant_language,
)
from jarvis.core.dialogue import Conversation
from jarvis.core.errors import STTError
from jarvis.core.faults import Faults
from jarvis.core.meter import Meter
from jarvis.core.pending import TTL as PENDING_TTL
from jarvis.core.persona import DONE, FAILED, LISTENING, WORKING, Persona
from jarvis.core.router import Dispatcher
from jarvis.core.state import DEAF, Modes, wakes_up
from jarvis.core.stt import STT
from jarvis.core.stt.stream import STTStream
from jarvis.core.text.sentences import SentenceSplitter
from jarvis.core.tts import TTS

logger = logging.getLogger(__name__)

#: Сколько фрагментов держать в очереди на распознавание, если в конфиге
#: не сказано иное. Больше двух держать смысла мало: распознавание идёт
#: примерно в реальном времени, и очередь означает, что команда выполнится
#: с опозданием на её длину.
_PENDING_LIMIT = 2

#: Откуда пришло «ответил» после заполнителя «секунду»: это речь, но не ответ.
FILLER_SOURCE = "voice.filler"

#: Имя, пойманное позже этого от начала фразы, при том что в расшифровке имени
#: нет, — не обращение, а слово из песни или чужого разговора (14.09.2026).
#: Замер по логам 12–14.09: у всех 8 настоящих команд без узнанного имени
#: («Реза откройфанель», «Транс останови…») детектор срабатывал не позже чем
#: через секунду после начала речи; у ложных («Come from…», «Да, я уже поел»,
#: «Алесса, люблю тебя») зазор 1–13 с, у 7 из 12 — от трёх. Имя произносят
#: первым, поэтому запоздалое срабатывание — улика. Фраза, где имя расслышано и
#: в тексте, этим правилом не затрагивается вовсе.
LATE_NAME_S = 2.0

#: Просьбы замолчать. Сверяются целиком: «стоп» внутри фразы — уже команда
#: («поставь на стоп»), и её разбирает роутер.
HUSH_PHRASES = frozenset({
    "стоп", "хватит", "замолчи", "замолкни", "тихо", "тише не надо", "заткнись",
    "помолчи", "молчи", "всё хватит", "все хватит", "стоп стоп", "хорош",
    "stop", "shut up", "enough", "be quiet",
})


def is_hush(text: str) -> bool:
    """Просьба замолчать, сказанная сама по себе."""
    return " ".join(text.lower().replace("ё", "е").strip(" .,!?…").split()) in {
        phrase.replace("ё", "е") for phrase in HUSH_PHRASES
    }


def level_db(data: bytes) -> float:
    """Громкость куска PCM в дБ от полной шкалы; тишина — -120."""
    count = len(data) // 2
    if not count:
        return -120.0
    samples = memoryview(data[: count * 2]).cast("h")
    power = sum(sample * sample for sample in samples) / count
    return 10 * math.log10(power / 32768**2) if power else -120.0


def speech_level_db(audio: bytes, frame_bytes: int) -> float:
    """Громкость речи во фразе: громкие кадры (верхняя пятая часть), а не среднее
    с паузами — иначе тихая длинная фраза выглядела бы громче короткой громкой."""
    levels = sorted(level_db(audio[i : i + frame_bytes]) for i in range(0, len(audio), frame_bytes))
    if not levels:
        return -120.0
    return levels[int(len(levels) * 0.8)]


def foreign_script(text: str) -> bool:
    """Текст написан не кириллицей и не латиницей — не на языке ассистента.

    Считаются только буквы; меньше половины своих — чужой алфавит. Цифры и знаки
    не решают ничего: «ES2», «7-8» остаются своими.
    """
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return False
    own = sum(1 for char in letters if "a" <= char.lower() <= "z" or "а" <= char.lower() <= "я" or char.lower() == "ё")
    return own * 2 < len(letters)

#: Похоже на имя, но недостаточно, чтобы счесть обращением. Нужен только для
#: подсказки в логе: иначе непонятно, почему ассистент промолчал.
ALMOST_NAME = 0.45

#: Второе подряд написание имени в расшифровке. Порог ниже основного: первое
#: слово уже опознано как имя, и обычным словам команд тут взяться неоткуда —
#: замер на ослышках из живого лога дал им 0.5–0.9 против 0.4 и ниже у команд.
DOUBLED_NAME = 0.55


def _bare(word: str) -> str:
    """Слово без знаков препинания и регистра — так его и сравнивают с именем."""
    return word.lower().strip(" .,!?;:—-")


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
        announcer: "Announcer | None" = None,
        meter: "Meter | None" = None,
        conversation: "Conversation | None" = None,
        hotwords: Any = None,
        recorder: Any = None,
        faults: Faults | None = None,
        reply_language: str = "",
    ) -> None:
        #: Журнал сбоев обращения к модели: неудача называет причину, а не
        #: прячется за «не справился» (21.09.2026).
        self._faults = faults if faults is not None else Faults()
        #: Отвечать только на этом языке (`app.reply_language`); пусто — на
        #: языке вопроса.
        self._reply_language = reply_language
        #: Детектор слов без имени — только замер: пишет в лог, ничего не делает.
        self._hotwords = hotwords
        #: Запись услышанных фраз на диск (`audio.record_dir`); ``None`` — не писать.
        self._recorder = recorder
        self._source = source
        self._sink = sink
        self._vad = vad
        self._wake_word = wake_word
        self._stt = stt
        #: Чем открыть потоковое распознавание фразы; ``None`` — движок не умеет
        #: (Whisper, заглушка), и фраза идёт целиком, как раньше.
        opener = getattr(stt, "open_stream", None)
        self._open_stream = opener if callable(opener) else None
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
        #: Политика речи без вопроса. Конвейеру она нужна не чтобы решать, а
        #: чтобы **досказывать**: придержанное произносится при первом же
        #: разговоре, когда владелец заведомо рядом и слушает.
        self._announcer = announcer if announcer is not None else Announcer()
        #: Учёт процессорного времени по звеньям. Выключенный не стоит ничего.
        self._meter = meter if meter is not None else Meter(enabled=False)
        #: Недавний разговор. Конвейер — единственное место, куда сходятся оба
        #: входа (голос и клавиатура), поэтому запоминает реплики он: заводить
        #: свою историю каждому входу значило бы, что ассистент помнит сказанное
        #: голосом и не помнит напечатанное минуту назад.
        self._conversation = (
            conversation if conversation is not None else Conversation()
        )

        # Вместе со звуком храним момент, когда он прозвучал: окно ответа
        # должно отсчитываться от речи, а не от того, когда до неё дошли руки.
        self._pending: asyncio.Queue[tuple[bytes, float, STTStream | None]] = asyncio.Queue(
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
        #: Когда детектор услышал имя, открывшее окно. Нужно, чтобы отличить
        #: имя в начале фразы от «имени», пойманного посреди песни (`LATE_NAME_S`).
        self._name_heard_at = 0.0
        #: Уровень фона микрофона в дБ; ``None`` — ещё не слышали тишины.
        self._floor_db: float | None = None
        #: Последняя разобранная фраза прошла без имени в тексте, а детектор
        #: сработал посреди неё. Такой фразе команда разрешена, разговор — нет.
        self._unnamed = False
        #: Язык разговора. Держится, пока его явно не сменят: в русской просьбе
        #: латиницей пишут названия программ и файлов, и считать их сменой языка
        #: значит отвечать по-английски на русский вопрос.
        self._language = ""
        #: Слушать ли имя по звуку. Признак — не режим из конфига, а то,
        #: поднялась ли настоящая модель: заглушка в этом слоте отвечает «да»
        #: на любой кадр, и спрашивать её означало бы срабатывать всегда.
        self._acoustic = not isinstance(wake_word, AlwaysActiveWakeWord)
        #: Пускать в распознавание только сказанное после имени. Без настоящей
        #: акустической модели бессмысленно: звать было бы нечем, и ассистент
        #: оглох бы совсем.
        self._gate = self._acoustic and config.wake_word.recognize_after_name
        self._speaking = False
        self._mute_until = 0.0
        #: Отклик на распознанную команду: PCM и частота, либо None.
        self._activation: tuple[bytes, int] | None = None
        #: Ссылку держим, чтобы задачу не собрал сборщик мусора на полпути.
        self._sound_task: asyncio.Task[None] | None = None
        #: Фразы (по моменту начала), на которые отклик уже прозвучал при сабмите.
        self._early_ack: set[float] = set()
        #: Говорим по одной реплике за раз. Пока ответы шли только на команды,
        #: очередь получалась сама собой; напоминание же срабатывает когда
        #: угодно, в том числе посреди ответа, — и две реплики полезли бы в
        #: динамик одновременно.
        self._voice = asyncio.Lock()
        #: Просили замолчать: недоговорённый ответ бросается, звук обрывается.
        self._hush = asyncio.Event()
        #: Подписка на просьбы что-нибудь произнести; снимается при остановке.
        self._announcements: Any = None
        #: Подписка на команды со стороны (клавиатура, позже — Telegram).
        self._typed: Any = None

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
        # Кто-то может захотеть заговорить без вопроса — например напоминание.
        # Произносить обязаны мы: только тут микрофон глохнет на время речи.
        self._announcements = self._events.subscribe(
            AnnouncementRequested.NAME, self._announce
        )
        # Команда со стороны идёт тем же путём, что и голос: тут диспетчер,
        # персона и приглушение микрофона, второй такой набор заводить незачем.
        self._typed = self._events.subscribe(CommandTyped.NAME, self._on_typed)
        phrase = self._config.wake_word.phrase
        if self._acoustic:
            logger.info("Слушаю. Имя «%s» ловлю моделью, по звуку", phrase)
        elif self._config.wake_word.mode in ("text", "acoustic"):
            logger.info("Слушаю. Обращение по имени: «%s»", phrase)
        else:
            logger.info("Слушаю. Реагирую на любую распознанную фразу")

    async def stop(self) -> None:
        """Остановить прослушивание и разбор."""
        if self._announcements is not None:
            self._announcements.unsubscribe()
            self._announcements = None
        if self._typed is not None:
            self._typed.unsubscribe()
            self._typed = None
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

    async def _announce(self, event: Event) -> None:
        """Произнести то, о чём попросил скилл.

        Единственная точка, где реплика приходит не в ответ на команду.
        Микрофон при этом глохнет так же, как на любой другой речи, — иначе
        ассистент услышит собственное напоминание и, попав в окно ответа,
        честно попробует выполнить его как команду.

        Событие приходит общим типом: шина зовёт обработчик по **имени**
        события, а не по классу, и знать про наш класс не обязана. Сужаем сами
        — заодно это защита от чужого события с тем же именем.
        """
        if not isinstance(event, AnnouncementRequested):
            return
        # Обращение подставляет персона: скилл пишет «…, {address}», а «сэр»
        # это или имя — решает одно место на всю систему, как и везде.
        await self._say(self._persona.fill(event.text, event.language), language=event.language)

    async def _on_typed(self, event: Event) -> None:
        """Выполнить команду, пришедшую вводом, и ответить вслух.

        Ровно тот же путь, что у голоса: `handle` проведёт текст через роутер,
        озвучит ответ и приглушит на это время микрофон. Никакого особого пути
        для клавиатуры нет намеренно — «новый вход не даёт новых прав».

        Режим «не слушаю» здесь не проверяется: наблюдатель гасит себя сам, ещё
        до события, чтобы зря не гонять команду через роутер. Сужаем тип по той
        же причине, что и у объявлений: шина зовёт по имени события.
        """
        if not isinstance(event, CommandTyped):
            return
        text = event.text.strip()
        if not text:
            return
        source = event.source or "keyboard"
        logger.info("Команда текстом (%s): %r", source, text)
        await self.handle(
            Utterance(text=text, language=event.language, source=source)
        )

    # --- общий путь для голоса и текста ------------------------------------

    async def handle(self, utterance: Utterance) -> ToolResult:
        """Провести реплику через роутер и озвучить ответ.

        Обращение по имени вырезается независимо от источника: и «Джарвис,
        включи свет» из микрофона, и то же самое текстом должны попасть в
        роутер как «включи свет».
        """
        _, command = self._strip_wake(utterance.text)
        if is_hush(command):
            # Мимо роутера и мимо очереди голоса: пока ответ звучит, голос
            # занят, и просьба замолчать дождалась бы конца того, что обрывает.
            self.interrupt()
            return ToolResult.success({"hushed": True}, tool="")
        if command != utterance.text:
            utterance = Utterance(
                text=command,
                language=utterance.language,
                confidence=utterance.confidence,
                source=utterance.source,
                named=utterance.named,
            )
        # Разговор запоминается **до** выполнения: пока команда идёт, ассистент
        # уже должен знать, о чём речь, — иначе доклад фоновой задачи или
        # сработавшее напоминание придут в разговор, где последней реплики нет.
        self._conversation.said(utterance.text)
        # Конвейер умеет произносить ответ по ходу написания — объявляем это
        # инструменту. Задача в `_run` копирует контекст при создании, поэтому
        # ставится до неё и снимается после.
        live = LIVE_SPEECH.set(True)
        try:
            result = await self._run(utterance)
        finally:
            LIVE_SPEECH.reset(live)
        # Диспетчер решил, что это было не к нам (слова из песни, чужой разговор):
        # молчим совсем — «Готово» в ответ на «люблю тебя» хуже тишины.
        if isinstance(result.value, dict) and result.value.get("ignored"):
            self._events.emit(WakeDismissed(source="voice", text=utterance.text, reason=str(result.value["ignored"])))
            return result
        if result.speech_stream is not None:
            reply = await self._say_stream(result.speech_stream, language=utterance.language)
        else:
            # Вариант выбирает персона, а не скилл: она помнит, что уже говорила,
            # и у каждой команды своя память — «пауза» не вытесняет «включаю».
            options = result.speech_options(utterance.language)
            reply = self._persona.choose(
                result.tool or "tool", options, utterance.language
            ) or self._describe(result, utterance.language)
            if reply:
                await self._say(reply, language=utterance.language)
        if reply:
            # В разговор идёт только ответ на команду. Речь без вопроса —
            # реакции на набранное, напоминания, приветствие — сюда не пишется:
            # ироничных реплик за вечер десятки, и они вытеснили бы из короткой
            # памяти то единственное, ради чего она заведена.
            self._conversation.replied(reply)

        if result.confirm is not None or result.choices:
            self._await_answer()
        else:
            # Владелец сам заговорил — значит он рядом и слушает. Лучшего
            # момента досказать придержанное не будет: будить его ради
            # накопленных новостей было бы ровно тем, от чего политика и
            # защищает. Во время незакрытого вопроса молчим: вклиниваться со
            # сторонними новостями между вопросом и ответом — верный способ
            # сбить человека.
            self._announcer.flush(language=utterance.language or "ru")
        return result

    def _await_answer(self) -> None:
        """Открыть окно пошире: ассистент сам задал вопрос и ждёт ответа.

        Обычные десять секунд тут не годятся. Человеку, которого спросили
        «отправить маме?», надо успеть подумать, а требовать при этом снова
        звать по имени — значит сделать переспрашивание неудобнее, чем просто
        повторить команду, то есть бессмысленным.

        Отсчёт от `_mute_until`, а не от «сейчас», по той же причине, что и у
        окна после «Слушаю»: пока вопрос звучит, время идти не должно.
        """
        self._follow_up_until = max(
            self._follow_up_until, self._mute_until + PENDING_TTL
        )
        logger.info("Задал вопрос, жду ответа без имени %.0f с", PENDING_TTL)

    async def _run(self, utterance: Utterance) -> ToolResult:
        """Выполнить команду, а если она затянулась — сказать, что работаем.

        Молчащий несколько секунд ассистент неотличим от зависшего, и человек
        начинает повторять команду. Повтор уходит в роутер вторым разом, то есть
        плата за молчание не только в нервах: «включи музыку», сказанное дважды,
        выполнится дважды.

        **Заполнитель стоит времени, и это честный размен.** Голос один и
        занимается по очереди, поэтому настоящий ответ подождёт, пока «секунду»
        договорит. Отсюда высокий порог: заполнитель должен срабатывать там, где
        пауза и так выглядит зависанием, а не на каждой команде. Короткие фразы
        из шаблонов до него не доживают вовсе.

        Отменить уже начатую реплику нельзя, но можно не начинать: если работа
        закончилась, пока мы ждали своей очереди у замка, говорить «секунду» уже
        поздно и незачем.
        """
        delay = self._config.working_after_s
        work = asyncio.ensure_future(self._dispatcher.handle(utterance))
        if delay <= 0 or self.silent:
            return await work

        try:
            return await asyncio.wait_for(asyncio.shield(work), delay)
        except TimeoutError:
            pass

        if not work.done() and not self._muted:
            filler = self._persona.line(WORKING, utterance.language)
            async with self._voice:
                if not work.done():
                    await self._speak(
                        filler, language=utterance.language, remember=False
                    )
        return await work

    def interrupt(self) -> bool:
        """Замолчать сейчас же: оборвать звук и бросить недоговорённое.

        Просьба владельца 19.09.2026: ослышался — и читает стену текста, а
        остановить нечем. Голосом перебить нельзя: на время своей речи микрофон
        заглушён, а детектор имени на его же голосе срабатывает ложно (замер:
        44 раза на 292 репликах из кеша). Поэтому просят клавишей, меню трея
        или словом «стоп» текстом.

        :return: было ли что обрывать.
        """
        if not self._speaking and not self._voice.locked():
            return False
        logger.info("Замолкаю по просьбе")
        self._hush.set()
        if isinstance(self._sink, InterruptibleSink):
            self._sink.interrupt()
        return True

    async def _say(self, text: str, *, language: str | None = None) -> None:
        """Озвучить реплику, заглушив на это время микрофон.

        Без этого получается акустическая петля: колонки произносят ответ,
        микрофон его слышит, Whisper расшифровывает — и ассистент разбирает
        собственную реплику как команду. В окне ответа, где имя не требуется,
        он её ещё и выполнит.
        """
        if not text:
            return
        # По одной реплике за раз. Ответ на команду и сработавшее напоминание
        # приходят из разных мест и запросто совпадают по времени; без очереди
        # они полезли бы в динамик вместе, а микрофон разглох бы посреди первой.
        async with self._voice:
            self._hush.clear()
            await self._speak(text, language=language)

    async def _say_stream(self, pieces: AsyncIterator[str], *, language: str | None) -> str:
        """Произнести ответ, который модель ещё пишет, — по предложению.

        Первое предложение звучит, пока пишутся остальные: ответ в секунду
        длиной начинает звучать с первым законченным предложением, а не когда
        дописан весь (просьба владельца 14.09.2026 «отвечать моментально»).
        Следующие предложения синтезируются заранее, пока звучит текущее, —
        иначе между ними была бы пауза на синтез.

        Голос занят на весь ответ целиком: напоминание, влезшее между
        предложениями, разорвало бы фразу пополам. «Ответил» сообщается один
        раз, в конце, — с полным текстом.

        :return: весь произнесённый ответ; оборвался до первого предложения —
            реплика о неудаче.
        """
        splitter = SentenceSplitter()
        ready: asyncio.Queue[tuple[str, asyncio.Task[None] | None] | None] = asyncio.Queue()

        def queue(sentence: str, first: bool) -> None:
            # Первое не готовим: оно звучит сразу, потоком синтеза.
            warm = None if first or self.silent else asyncio.create_task(
                self._tts.prewarm(sentence, language=language)
            )
            ready.put_nowait((sentence, warm))

        async def write() -> None:
            first = True
            try:
                async for piece in pieces:
                    for sentence in splitter.push(piece):
                        queue(sentence, first)
                        first = False
                tail = splitter.flush()
                if tail:
                    queue(tail, first)
            except Exception as exc:  # noqa: BLE001 — произнесённое уже не вернуть
                self._faults.note(exc)
                logger.error("Ответ модели оборвался (%s): %s", type(exc).__name__, exc)
            finally:
                ready.put_nowait(None)

        said: list[str] = []
        writer = asyncio.create_task(write())
        async with self._voice:
            self._hush.clear()
            while (item := await ready.get()) is not None:
                sentence, warm = item
                if self._hush.is_set():
                    break
                if warm is not None:
                    await warm
                await self._speak(sentence, language=language, replied=False)
                said.append(sentence)
        if self._hush.is_set():
            writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)

        if not said:
            reply = self._excuse(language)
            await self._say(reply, language=language)
            return reply
        reply = " ".join(said)
        self.last_reply = reply
        self._events.emit(
            AssistantReplied(source="voice", text=reply, spoken=not self.silent and self._tts.ready)
        )
        return reply

    async def _speak(
        self,
        text: str,
        *,
        language: str | None = None,
        remember: bool = True,
        replied: bool = True,
    ) -> None:
        """Собственно озвучка — вызывается только из `_say`, под замком.

        :param remember: считать ли это ответом на команду. Заполнитель
            «секунду» произносится вслух, но ответом не является: `--say`
            печатает `last_reply`, и напечатать «секунду» вместо результата
            значило бы соврать о том, чем всё кончилось.
        :param replied: сообщать ли, что ответ отзвучал. Предложение из
            середины ответа — ещё не конец: по «ответил» скилл windows
            возвращает громкость, и музыка поднималась бы между предложениями.
        """
        # Что именно сказал ассистент, по логу иначе не восстановить: в нём
        # видно команду и её результат, а произнесённой фразы — нет. А разбирать
        # приходится как раз расхождение между ними.
        # `tone` — пометка для консоли: реплики разговора там ищут глазами
        # первыми. Помечаем полем записи, а не подсветкой по тексту сообщения:
        # угадывание рассыпалось бы при первой правке формулировки.
        logger.info("Отвечаю: %s", text, extra={"tone": "said"})
        if remember:
            self.last_reply = text
        if self.silent:
            # Голос выключен целиком: реплика уже в логе, а трогать синтез
            # нельзя — он загрузит модель при первом же обращении.
            if replied:
                self._events.emit(
                    AssistantReplied(source="voice" if remember else FILLER_SOURCE, text=text, spoken=False)
                )
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

        # Заполнитель («Один момент») — не ответ: по «ответил» скилл windows
        # возвращает громкость, и она поднималась поверх настоящего ответа,
        # звучавшего следом (живой запуск 14.09.2026, 15:34).
        if not replied:
            return
        self._events.emit(
            AssistantReplied(
                source="voice" if remember else FILLER_SOURCE, text=text, spoken=spoken and self._tts.ready
            )
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

    def _language_of(self, command: str, *, fallback: str = "") -> str:
        """На каком языке отвечать на эту команду.

        Язык разговора держится, пока его явно не сменили: латиница в названии
        программы, сайта или файла поводом не является. Подробности и цена
        ошибки — в `dominant_language`.
        """
        if self._reply_language:
            # Владелец задал язык ответа — догадки распознавания не в счёт.
            return self._reply_language
        found = dominant_language(command)
        if found:
            self._language = found
            return found
        return self._language or fallback or "ru"

    def _excuse(self, language: str | None) -> str:
        """Чем объяснить неудачу: свежим сбоем, если он есть, иначе вежливо.

        Слова берутся у персоны по виду сбоя: она умеет не повторяться и знает
        обращение. Вид сбоя — из журнала, провайдер вслух не называется: его имя
        человеку ничего не говорит, а чинить он идёт в панель и в лог.
        """
        fault = self._faults.recent()
        if fault is None:
            return self._persona.line(FAILED, language)
        return self._persona.choose(
            f"fault.{fault.kind}", self._persona.lines(fault.kind, language), language
        ) or self._persona.line(FAILED, language)

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
            # Причина сбоя важнее и собственного объяснения инструмента, и
            # вежливого отказа: «кончились деньги на счету» человек починит, а
            # «не справился» отправит искать поломку в коде.
            fault = self._faults.recent()
            if fault is not None:
                return self._excuse(language)
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
        #: Последние кадры до начала речи: детектор узнаёт о ней с опозданием.
        before: deque[bytes] = deque(maxlen=self._config.preroll_frames or 1)
        #: Потоковое распознавание текущей фразы; ``None`` — не открыто.
        stream: STTStream | None = None

        try:
            async for frame in self._source.frames():
                # Пока говорим сами — кадры читаем, но выбрасываем: иначе
                # очередь захвата забьётся собственной речью.
                if self._muted:
                    if speaking:
                        logger.debug("Свою речь не слушаю, накопленное отбрасываю")
                    if stream is not None:
                        stream.cancel()
                        stream = None
                    buffer.clear()
                    before.clear()
                    silence = 0
                    speaking = False
                    self._vad.reset()
                    continue

                if self._acoustic:
                    with self._meter.stage("имя"):
                        heard = self._wake_word.detect(frame)
                    if heard:
                        self._on_name_heard()
                with self._meter.stage("речь"):
                    speech = self._vad.is_speech(frame)

                if self._hotwords is not None:
                    # После детектора речи: по нему спотер решает, прозвучало ли
                    # слово отдельно — сам декодер на грамматике этого не видит.
                    with self._meter.stage("горячие слова"):
                        spotted = self._hotwords.feed(frame, speech=speech)
                    if spotted is not None:
                        self._note_hotword(spotted)
                if speech:
                    if not speaking:
                        logger.debug("Начало речи")
                        if self._config.preroll_frames:
                            buffer.extend(b"".join(before))
                        before.clear()
                    buffer.extend(frame.data)
                    stream = self._stream_utterance(buffer, stream, frame.data)
                    silence = 0
                    speaking = True
                    continue

                if not speaking:
                    before.append(frame.data)
                    self._note_floor(frame.data)
                    continue

                # Немного тишины оставляем в конце: Whisper лучше слышит границу.
                buffer.extend(frame.data)
                stream = self._stream_utterance(buffer, stream, frame.data)
                silence += 1

                too_long = len(buffer) >= self._config.max_utterance_bytes
                if silence >= self._config.silence_frames or too_long:
                    if too_long:
                        logger.debug("Фраза достигла предела длины, отправляю как есть")
                    self._record(bytes(buffer))
                    self._submit(bytes(buffer), stream)
                    stream = None
                    buffer.clear()
                    silence = 0
                    speaking = False
                    self._vad.reset()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Цикл прослушивания остановлен из-за ошибки")

    def _record(self, audio: bytes) -> None:
        """Сохранить фразу на диск, если просили, — в фоне, не задерживая слух."""
        if self._recorder is None:
            return
        spoken_at = time.time() - len(audio) / 2 / self._config.sample_rate
        named = spoken_at <= self._name_heard_at <= time.time()
        recorder = self._recorder

        def write() -> None:
            try:
                path = recorder.save(audio, spoken_at=spoken_at, named=named)
                logger.info("Записал фразу: %s", path.relative_to(recorder.directory.parent))
            except OSError as exc:
                logger.warning("Фраза не записалась: %s", exc)

        asyncio.get_running_loop().run_in_executor(None, write)

    def _note_floor(self, data: bytes) -> None:
        """Уровень фона: медленное среднее громкости кадров без речи, в дБ."""
        level = level_db(data)
        self._floor_db = level if self._floor_db is None else self._floor_db * 0.98 + level * 0.02

    def _note_hotword(self, spotted: Any) -> None:
        """Замер слов без имени: записать, что сработало бы. Ничего не выполнять.

        По этим строкам потом считается, сколько было настоящих команд и сколько
        ложных, и помогает ли правило «одно слово с тишиной вокруг»
        (15.09.2026, «механизм без замера не ставится»).
        """
        speech_ms = getattr(spotted, "speech_ms", None)
        word_ms = getattr(spotted, "word_ms", None)
        timing = ""
        if speech_ms is not None:
            timing = f", речи {speech_ms / 1000:.1f} с"
            if word_ms is not None:
                timing += f", слово {word_ms / 1000:.1f} с"
        logger.info(
            "Замер слов без имени: сработало бы «%s» (%s%s; гипотеза: %r)",
            ", ".join(spotted.words),
            "одно слово" if spotted.alone else "внутри фразы",
            timing,
            spotted.heard,
        )

    def _on_name_heard(self) -> None:
        """Модель активации услышала имя — ещё до всякого распознавания.

        Ради этого момента она и нужна: событие уходит в шину сразу, и музыку
        успевают приглушить **до** того, как команда прозвучит. Текстовый гейт
        такого не может в принципе — он узнаёт имя после Whisper, когда фраза
        уже записана вместе с фоном.

        Дальше всё идёт по накатанному: открывается то же окно, что и после
        голого «Джарвис», а имя из расшифровки снимет `_strip_wake`.
        """
        # Что детектор расслышал — до сброса, сброс это забудет. Число «(0.00)»
        # в логе раньше ничего не значило: оценка обнулялась тем же сбросом, а
        # разбирать ложные срабатывания нужно по тому, что именно услышано.
        heard = str(getattr(self._wake_word, "hypothesis", "") or "")
        self._wake_word.reset()
        # В режиме «не слушаю» имя ничего не открывает и никого не будит.
        # Событие отсюда приглушает музыку, и без этой проверки каждое
        # случайное «Джарвис» дёргало бы громкость у молчащего ассистента.
        if self._modes.active(DEAF):
            logger.debug("Услышал имя, но сейчас не слушаю")
            return
        now = time.time()
        # Запоминаем срабатывание, которое **открыло** окно. Повторное в уже
        # открытом окне («Джарвис Джарвис») отметку не сдвигает: иначе имя в
        # начале фразы выглядело бы запоздалым.
        if now >= self._follow_up_until:
            self._name_heard_at = now
        self._follow_up_until = now + self._config.wake_word.follow_up_s
        logger.info(
            "Услышал имя (детектор: %r) — жду команду %.0f с",
            heard or self._wake_word.phrase,
            self._config.wake_word.follow_up_s,
        )
        self._events.emit(
            WakeWordDetected(
                source="voice",
                phrase=self._wake_word.phrase,
                score=float(getattr(self._wake_word, "score", 1.0)),
            )
        )

    def _acknowledge_early(self, spoken_at: float) -> None:
        """Отозваться звуком сразу, как фраза кончилась, — не дожидаясь расшифровки.

        Раньше отклик играл после распознавания, то есть через полторы секунды
        после того, как человек замолчал (просьба владельца 14.09.2026: «отвечать
        моментально»). Звучит он только когда имя прозвучало **в начале** этой
        фразы: запоздалое имя — примета песни или чужого разговора
        (`LATE_NAME_S`), и пищать на них незачем. Цена: на ложном имени в первую
        секунду («Алесса, люблю тебя») отклик прозвучит — ответа при этом не будет.
        """
        if self._activation is None:
            return
        late = self._name_heard_at - spoken_at
        if not 0 <= late < LATE_NAME_S:
            return
        self._early_ack.add(spoken_at)
        self._sound_task = asyncio.create_task(self._play_activation())

    def _stream_utterance(
        self, buffer: bytearray, stream: STTStream | None, chunk: bytes
    ) -> STTStream | None:
        """Досылать фразу в потоковое распознавание — но только после имени.

        Пока ворота закрыты, звук копится у нас и в облако не уходит, как и
        раньше. Открылись (детектор услышал имя или открыто окно ответа) —
        поток открывается, первым куском уходит накопленное начало фразы, а
        дальше кадры идут по мере записи.
        """
        if stream is not None:
            stream.feed(chunk)
            return stream
        if self._open_stream is None:
            return None
        spoken_at = time.time() - len(buffer) / 2 / self._config.sample_rate
        if not self._worth_recognising(spoken_at):
            return None
        opened = self._open_stream(sample_rate=self._config.sample_rate)
        if opened is not None:
            opened.feed(bytes(buffer))
        return opened

    def _submit(self, audio: bytes, stream: STTStream | None = None) -> None:
        """Отправить фрагмент на распознавание, не блокируя захват.

        Вместе с фрагментом запоминается момент, когда он **начал** звучать.
        Без этого окно ответа проверялось бы по времени разбора, а между речью
        и разбором лежит распознавание — несколько секунд. Пока Whisper думал,
        окно успевало закрыться, и ответ на «Слушаю» терялся.
        """
        if len(audio) < self._config.min_utterance_bytes:
            logger.debug("Фрагмент слишком короткий (%d байт), пропускаю", len(audio))
            if stream is not None:
                stream.cancel()
            return
        spoken_at = time.time() - len(audio) / 2 / self._config.sample_rate
        if not self._worth_recognising(spoken_at):
            logger.debug(
                "Имени не было — фрагмент %.1f с не расшифровываю",
                len(audio) / 2 / self._config.sample_rate,
            )
            if stream is not None:
                stream.cancel()
            return
        speech_db = speech_level_db(audio, self._config.frame_bytes)
        if self._floor_db is not None:
            # Только числа, звук не сохраняется: по ним видно, в микрофоне ли
            # беда, когда расшифровка — каша (стенд 18.09.2026: при разнице
            # 20 дБ Deepgram слышит «Братарвис и daily cheese»).
            logger.info(
                "Уровень фразы: речь %.0f дБ, фон %.0f дБ, разница %.0f дБ",
                speech_db, self._floor_db, speech_db - self._floor_db,
            )
        if stream is not None:
            # Итог просим сразу, а не когда до фразы дойдёт очередь разбора: пока
            # выполняется прошлая команда, облако закрыло бы молчащий поток.
            stream.end()
        try:
            self._pending.put_nowait((audio, spoken_at, stream))
            self._acknowledge_early(spoken_at)
        except asyncio.QueueFull:
            if stream is not None:
                stream.cancel()
            # Чаще всего это не «медленный компьютер», а посторонняя речь:
            # видео в колонках или разговор рядом. Whisper распознаёт примерно
            # в реальном времени, поэтому непрерывный фон забивает очередь.
            logger.warning(
                "Не успеваю распознавать — фрагмент %.1f с отброшен. "
                "Если повторяется: посторонняя речь в микрофоне либо тяжёлая "
                "модель (stt.model)",
                len(audio) / 2 / self._config.sample_rate,
            )

    def _worth_recognising(self, spoken_at: float) -> bool:
        """Расшифровывать ли этот фрагмент вообще.

        Раньше в распознавание уходила **любая** речь в комнате, а имя искали
        уже в готовом тексте. Платили за это дважды: чужой разговор уезжал в
        облако, и за него шёл счёт. Теперь имя ловится по звуку **до** всякой
        расшифровки, и спрашивать текст незачем — достаточно знать, звали ли нас.

        Сравнение с окном ответа, а не с отдельной отметкой, потому что окно и
        открывается акустической моделью: одно и то же событие, и заводить
        второй счётчик того же смысла значило бы однажды их рассинхронизировать.

        Отсчёт от **начала** фрагмента: имя произносят первым, а окно
        открывается уже посреди фразы.
        """
        if not self._gate:
            return True
        return spoken_at < self._follow_up_until

    # --- распознавание и разбор --------------------------------------------

    async def _consume(self) -> None:
        """Разбирать накопленные фрагменты по одному."""
        while True:
            item = await self._pending.get()
            audio, spoken_at = item[0], item[1]
            stream = item[2] if len(item) > 2 else None
            try:
                await self._process(audio, spoken_at, stream)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Ошибка при разборе фрагмента")

    async def _process(self, audio: bytes, spoken_at: float, stream: STTStream | None = None) -> None:
        """Распознать фрагмент и обработать реплику.

        :param spoken_at: когда фраза прозвучала. Именно по этому времени
            проверяется окно ответа: распознавание идёт секунды, и сверяться
            с часами после него — значит закрывать окно раньше времени.
        :param stream: потоковое распознавание этой фразы, если открывалось.
            Сорвалось — та же фраза уходит обычным путём.
        """
        seconds = len(audio) / 2 / self._config.sample_rate
        started = time.perf_counter()
        transcript = None
        how = ""
        if stream is not None:
            try:
                transcript = await stream.finish()
                how = ", поток"
            except STTError as exc:
                logger.warning("Поток распознавания сорвался (%s) — распознаю фразу целиком", exc)
        if transcript is None:
            transcript = await self._stt.transcribe(audio, sample_rate=self._config.sample_rate)
        if transcript.empty:
            logger.debug("Фрагмент %.1f с не дал текста", seconds)
            return

        logger.info(
            "Распознано (%.1f с речи за %.1f с%s): %r",
            seconds,
            time.perf_counter() - started,
            how,
            transcript.text,
            extra={"tone": "heard"},
        )

        command = self._extract_command(transcript.text, spoken_at=spoken_at)
        if command is None:
            logger.debug("Обращения по имени нет — пропускаю")
            # Музыку приглушили по имени — сказать, что ответа не будет, иначе
            # она ждёт страховочного таймера (15.09.2026, 09:12: двадцать секунд).
            self._events.emit(WakeDismissed(source="voice", text=transcript.text, reason="не ко мне"))
            return

        # Язык ответа — по команде, а не по всей расшифровке: имя в ней бывает
        # записано латиницей («Jaris Jaris, как дела»), и к языку просьбы оно
        # отношения не имеет.
        #
        # И меняется он **только при явном перевесе**. Простого большинства не
        # хватило: «открой папку Photostock. Jpg» дало тринадцать латинских букв
        # против одиннадцати кириллических, и ассистент ответил «I don't know a
        # program called папку Photostock. Jpg». Названия программ и файлов
        # латиницей — обычное дело в русской просьбе, а не смена языка.
        language = self._language_of(command, fallback=transcript.language)

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
        # равно прозвучит после — динамик занят по очереди. Если отклик уже
        # прозвучал при конце фразы (`_acknowledge_early`), второй раз не нужен.
        if spoken_at in self._early_ack:
            self._early_ack.discard(spoken_at)
        else:
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
            Utterance(text=command, language=language, source="voice", named=not self._unnamed)
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

        first = _bare(words[0])
        remainder = " ".join(words[1:]).strip(" ,")

        if first in settings.aliases or first in settings.phrases:
            logger.debug("Имя распознано: %r", first)
            return True, self._drop_doubled_name(remainder)

        ratio = self._like_name(first)
        if ratio >= settings.similarity:
            logger.debug("Имя распознано: %r (похожесть %.2f)", first, ratio)
            return True, self._drop_doubled_name(remainder)

        # Почти совпало — скорее всего звали, но модель ослышалась.
        # Показываем на уровне INFO: иначе непонятно, почему ассистент молчит.
        if ratio >= ALMOST_NAME:
            logger.info(
                "Похоже на обращение, но не уверен: %r ~ %r (%.2f). "
                "Добавь вариант в audio.wake_word.aliases, если повторяется",
                first,
                settings.phrase,
                ratio,
            )
        return False, cleaned

    def _like_name(self, word: str) -> float:
        """Насколько слово похоже на имя.

        Сравниваем с каждым написанием: «jarvis» и «джарвис» — одно и то же
        имя в разных алфавитах, а похожесть между ними нулевая.
        """
        return max(
            (
                difflib.SequenceMatcher(None, word, phrase).ratio()
                for phrase in self._config.wake_word.phrases
            ),
            default=0.0,
        )

    def _drop_doubled_name(self, command: str) -> str:
        """Убрать второе написание имени, если распознавание выдало его дважды.

        Имя при этом произносят **один раз** — двоится расшифровка. На плохо
        расслышанном коротком обращении Deepgram выдаёт сразу две догадки одного
        и того же слова подряд: «Jaris Jaris, как дела», «Тарвис Тарвис на
        паузу», «Дарвис Драйвис», «Джарвис Прарвисская дела». Длина куска при
        этом остаётся прежней — полторы-две секунды, на два «Джарвиса» там места
        нет. Подсказка словаря (`stt.keyterms`) ни при чём: первый такой случай
        в логе на полтора часа старше её.

        Первое написание снимает `_strip_wake`, второе оставалось в команде, и
        роутер получал мусор: «Прарвисская дела» не узнаёт ни один шаблон, зато
        за неё платят модели.

        Порог тут ниже основного намеренно. Первое слово **уже опознано** как
        имя, поэтому второе похожее — почти наверняка оно же, а не начало
        команды: на замере ослышки имени лежат в 0.5–0.9, обычные слова команд
        не дотягивают и до 0.4. Убирается ровно одно слово: догадок бывает две,
        а не пять, и ошибочный порог не должен съесть фразу целиком.
        """
        words = command.split()
        if not words:
            return command

        head = _bare(words[0])
        settings = self._config.wake_word
        known = head in settings.aliases or head in settings.phrases
        ratio = 1.0 if known else self._like_name(head)
        if ratio < DOUBLED_NAME:
            return command

        logger.info("Имя в расшифровке двоится, второе убрал: %r (%.2f)", head, ratio)
        return " ".join(words[1:]).strip(" ,")

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

    def _not_for_me(self) -> None:
        """Ложное «имя» разоблачено — закрыть и окно ответа, которое оно открыло.

        Живой случай 26.09.2026, 00:52: детектор услышал «джарвис» в чужом
        разговоре, фраза «Братан, с тобой лежит» верно ушла в «не ко мне», а окно
        осталось открытым — и следующий обрывок чужой речи «Всеми-» прошёл как
        команда без имени: запрос к модели, «Секунду, сэр» и справка вслух.
        Окно здесь открыл именно этот детектор (иначе запоздания не было бы),
        так что закрывать его нечему помешать.
        """
        self._follow_up_until = 0.0

    def _extract_command(self, text: str, *, spoken_at: float | None = None) -> str | None:
        """Решить, обращались ли к ассистенту, и вернуть команду.

        :param spoken_at: когда фраза прозвучала; по умолчанию — сейчас
            (текстовый ввод приходит без задержки на распознавание).
        :return: текст команды; пустая строка, если позвали только по имени;
            ``None``, если обращения не было и окно ответа закрыто.
        """
        self._unnamed = False
        if self._modes.active(DEAF):
            return self._deaf_gate(text)

        called, command = self._strip_wake(text)

        if self._config.wake_word.mode not in ("text", "acoustic"):
            return command if called else text.strip()

        if called:
            return command

        moment = time.time() if spoken_at is None else spoken_at
        if moment < self._follow_up_until:
            late = self._name_heard_at - moment
            if spoken_at is not None and late >= LATE_NAME_S:
                logger.info(
                    "Имя поймано через %.1f с после начала фразы, а в тексте его нет — "
                    "не ко мне: %r",
                    late,
                    text,
                )
                self._not_for_me()
                return None
            if spoken_at is not None and late >= 0:
                # Детектор сработал посреди этой самой фразы, а в тексте имени нет.
                # Хинди в распознавании («चाहिए जल्दी जल्दी») — это не команда ни на
                # одном из двух языков ассистента: чужая речь или песня.
                if foreign_script(command):
                    logger.info("Имя поймано посреди фразы, а текст не русский и не английский — не ко мне: %r", text)
                    self._not_for_me()
                    return None
                self._unnamed = True
            logger.debug(
                "Окно ответа открыто ещё %.1f с — имя не требуется",
                self._follow_up_until - moment,
            )
            return command

        return None
