"""Активация по звуку: имя ловится до распознавания, а не после.

Сейчас имя ищется по тексту: фразу целиком расшифровывает Whisper, а потом
смотрим, начата ли она с «Джарвис». Это дёшево и работает, но у такого способа
есть предел, который упирается прямо в жизнь под музыку:

* **узнать имя можно только после конца фразы.** Пока Whisper считает, команда
  уже произнесена, и приглушать музыку поздно — она попала в запись целиком;
* **в шуме Whisper врёт именно на имени.** Оно короткое, редкое и стоит первым,
  то есть в самой невыгодной позиции.

Движка два, и выбор между ними — не вкусовщина.

**Vosk (по умолчанию).** Обычное распознавание, которому оставили словарь из
одного слова: декодер ищет «джарвис» и складывает всё прочее в «[unk]». Учить
нечего — русская модель готовая, 45 МБ, поднимается за 0.7 с и считает в
тридцать-шестьдесят раз быстрее реального времени.

**openWakeWord.** Классификатор поверх речевых эмбеддингов, 1.2 МБ. Тратит
меньше, но требует **своей обученной модели**: готовая `hey_jarvis` из их набора
на русское произношение не отзывается вовсе. Замерено 04.09.2026 на синтезе
теми голосами, которыми ассистент говорит сам: английское «Hey Jarvis» даёт
0.998, а русское «Джарвис» — 0.001, 0.000 и 0.076 у трёх дикторов. Модель учили
на двухсловной английской фразе, и русская фонетика ей чужая. Обучение своей
требует Linux с видеокартой NVIDIA (`docs/wakeword.md`), которой у владельца
нет, — поэтому путь через Vosk и стал основным.

Ни модели, ни пакета — режим не включается, и ассистент продолжает жить на
текстовом гейте. Это правило общее для всех тяжёлых адаптеров: отсутствие
модели ломать запуск не должно.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from jarvis.core.errors import AudioError

from .protocol import AudioFrame

logger = logging.getLogger(__name__)

#: Сколько сэмплов openWakeWord ждёт за раз: 80 мс на 16 кГц. Кадр захвата
#: короче (30 мс), поэтому кадры пересобираются здесь же — по той же причине,
#: что и у Silero: длина кадра принадлежит захвату, а не модели.
CHUNK_SAMPLES = 1280

#: Порог срабатывания по умолчанию. Ошибка в одну сторону — ассистент
#: откликается на постороннее слово, в другую — не откликается вовсе.
DEFAULT_THRESHOLD = 0.5


class OpenWakeWord:
    """Детектор активационной фразы по звуку."""

    def __init__(
        self,
        model: Path,
        *,
        phrase: str = "джарвис",
        sample_rate: int = 16000,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        if sample_rate != 16000:
            raise AudioError(
                f"openWakeWord работает на 16 кГц, а в конфиге {sample_rate}. "
                f"Поставь audio.sample_rate: 16000."
            )
        if not model.is_file():
            raise AudioError(
                f"Нет модели активации: {model}. Как её обучить — docs/wakeword.md, "
                f"либо верни audio.wake_word.mode: text."
            )

        try:
            from openwakeword.model import Model
        except ImportError as exc:  # pragma: no cover — зависит от установки
            raise AudioError(
                "Для активации по звуку нужен openwakeword: pip install -e \".[wakeword]\""
            ) from exc

        try:
            import numpy
        except ImportError as exc:  # pragma: no cover — зависит от установки
            raise AudioError("Для активации по звуку нужен numpy") from exc

        self._numpy = numpy
        self._model = Model(wakeword_models=[str(model)], inference_framework="onnx")
        self._phrase = phrase
        self._threshold = threshold if threshold > 0 else DEFAULT_THRESHOLD
        self._buffer = bytearray()
        self._score = 0.0
        logger.info(
            "Активация по звуку: %s, порог %.2f", model.name, self._threshold
        )

    @property
    def phrase(self) -> str:
        """Фраза активации — для логов и событий."""
        return self._phrase

    @property
    def score(self) -> float:
        """Насколько уверенно сработало в последний раз."""
        return self._score

    def detect(self, frame: AudioFrame) -> bool:
        """Прозвучало ли имя.

        Кадр короче куска модели, поэтому решение принимается не на каждом
        кадре. Ответ ``False`` означает «пока нет», а не «точно нет».
        """
        self._buffer.extend(frame.data)
        window = CHUNK_SAMPLES * 2
        heard = False

        while len(self._buffer) >= window:
            chunk = bytes(self._buffer[:window])
            del self._buffer[:window]
            samples = self._numpy.frombuffer(chunk, dtype=self._numpy.int16)
            scores: dict[str, Any] = self._model.predict(samples)
            best = max((float(value) for value in scores.values()), default=0.0)
            self._score = best
            if best >= self._threshold:
                heard = True

        return heard

    def reset(self) -> None:
        """Забыть накопленное после срабатывания.

        Без этого одно слово срабатывает несколько раз подряд: внутри у модели
        своя история, и имя остаётся в ней ещё на секунду.
        """
        self._buffer.clear()
        self._score = 0.0
        reset = getattr(self._model, "reset", None)
        if callable(reset):
            reset()


#: Что декодер отдаёт вместо слов, которых нет в словаре. Слово служебное:
#: оно и есть «всё остальное», без него декодер пытался бы услышать имя в
#: любом шуме.
UNKNOWN = "[unk]"

#: Из чего состоят слова, которые русская модель может знать. Латиница в
#: словаре не живёт: `jarvis` декодер отбрасывает молча, и написание, которое
#: выглядит рабочим, не срабатывает ни разу.
_CYRILLIC = frozenset("абвгдеёжзийклмнопрстуфхцчшщъыьэюя-")

#: Сколько миллисекунд имя должно продержаться в гипотезе, чтобы поверить.
#:
#: Без выдержки промежуточный результат врёт: декодер сперва предполагает имя,
#: а к концу фразы исправляется. Замерено 04.09.2026 покадрово — и разница
#: оказалась ровной, а не пограничной. **Ложная гипотеза живёт восемь кадров
#: по 30 мс и отменяется**: так «turn on the music» и «поставь на паузу
#: пожалуйста» на мгновение становились именем. **Настоящее имя держится от
#: двенадцати кадров и до конца записи** — то есть уже не отменяется никогда.
#:
#: Триста миллисекунд стоят ровно между этими числами. Задержка того стоит:
#: имя всё равно опознаётся к 0.7 с, за секунду с лишним до конца команды
#: «Джарвис, включи музыку», — а ради этого запаса всё и делалось.
HOLD_MS = 300.0

#: Сколько звука живёт один декодер, прежде чем его заменят новым.
#:
#: Декодер дорожает с возрастом: в живом логе 14.09.2026 нагрузка имени росла
#: с 3% ядра до 41% за двадцать минут и падала **только** после «Услышал имя» —
#: а срабатывание и создаёт новый декодер. Замер на пяти минутах «музыки» без
#: пауз: без замены 2.8 → 7.7 → 10.4 → 14.5% ядра по минутам; сброс по концу
#: фразы не помог ничем (14.4%); новый декодер раз в 8 с — ровно 2.0–2.3%,
#: раз в 30 с — 2.5–2.9%. «Джарвис» длится меньше секунды, запас десятикратный.
REFRESH_MS = 8000.0


def known_words(phrases: Sequence[str]) -> list[str]:
    """Отобрать написания, которые декодеру можно предложить.

    Слова не из словаря модели Vosk отбрасывает сам, с предупреждением в свой
    лог — а лог его мы гасим, потому что мимо словаря тут всё, кроме имени.
    Поэтому отбираем заранее и по понятному правилу: русское слово может
    оказаться в словаре, латинское — нет никогда.
    """
    return [
        word
        for phrase in phrases
        if (word := " ".join(str(phrase).lower().split()))
        and set(word) <= _CYRILLIC | {" "}
    ]


class VoskWakeWord:
    """Активация по звуку через распознавание со словарём из одного слова.

    Обучать нечего: модель русская и готовая, а ограничение словаря — штатная
    возможность декодера. Выигрыш от такого ограничения двойной: искать не из
    чего, поэтому быстро, и выдумать нечего, поэтому мало ложных.

    **Срабатывание идёт по частичному результату, а не по концу фразы.** Ждать
    конца нельзя: ради того всё и затевалось, чтобы приглушить музыку **до**
    команды. Замерено на синтезе: имя ловится на 0.4 с — раньше, чем оно
    дозвучало, и за полторы секунды до конца «Джарвис, включи музыку».
    """

    def __init__(
        self,
        model: Path,
        *,
        phrases: Sequence[str] = ("джарвис",),
        sample_rate: int = 16000,
        hold_ms: float = HOLD_MS,
    ) -> None:
        #: Выдержка имени в гипотезе — `audio.wake_word.hold_ms`.
        self._hold_ms = float(hold_ms)
        if not model.is_dir():
            raise AudioError(
                f"Нет модели активации: {model}. Скачается сама при запуске, "
                f"либо верни audio.wake_word.mode: text."
            )

        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel
        except ImportError as exc:  # pragma: no cover — зависит от установки
            raise AudioError(
                'Для активации по звуку нужен vosk: pip install -e ".[wakeword]"'
            ) from exc

        # Декодер разговорчив: на каждое слово мимо словаря он пишет свою
        # строку, а мимо словаря тут всё, кроме имени.
        SetLogLevel(-1)

        self._words = tuple(dict.fromkeys(known_words(phrases)))
        if not self._words:
            raise AudioError(
                f"Ни одно написание имени не годится для русской модели: "
                f"{', '.join(phrases) or '(пусто)'}. В словаре декодера живут "
                f"обычные русские слова — латиницу и выдуманные написания он "
                f"молча отбросит, и детектор не сработает ни разу."
            )

        self._rate = sample_rate
        self._model = Model(str(model))
        self._factory = KaldiRecognizer
        self._grammar = json.dumps([*self._words, UNKNOWN], ensure_ascii=False)
        self._recognizer = self._new()
        self._score = 0.0
        #: Сколько миллисекунд имя держится в гипотезе подряд.
        self._held_ms = 0.0
        #: Сработали ли уже на этой фразе.
        self._fired = False
        #: Сколько звука переварил текущий декодер.
        self._age_ms = 0.0
        logger.info(
            "Активация по звуку (vosk): %s, словарь: %s",
            model.name,
            ", ".join(self._words),
        )

    def _new(self) -> Any:
        """Создать декодер со словарём из имени и «всего остального».

        Слова, которых нет в словаре модели, декодер отбрасывает сам — с
        предупреждением, которое мы уже погасили. Поэтому написания можно
        перечислять смело: лишнее просто не попадёт в грамматику.
        """
        return self._factory(self._model, float(self._rate), self._grammar)

    @property
    def phrase(self) -> str:
        """Основное написание имени — для логов и событий."""
        return self._words[0]

    @property
    def hypothesis(self) -> str:
        """Что декодер расслышал в момент срабатывания — для разбора ложных срабатываний.

        Сам декодер знает только «джарвис» и «[unk]», так что ответ вида
        «[unk] джарвис [unk]» говорит, сколько чужих слов было вокруг имени.
        """
        return getattr(self, "_hypothesis", "")

    @property
    def score(self) -> float:
        """Насколько уверенно сработало в последний раз.

        У распознавания это не вероятность: декодер либо составил из звука
        слово, либо нет. Единица и ноль тут честнее выдуманной дроби.
        """
        return self._score

    def detect(self, frame: AudioFrame) -> bool:
        """Прозвучало ли имя.

        Ответ ``False`` означает «пока нет», а не «точно нет»: слово могло
        начаться в этом кадре, опознаться в следующем и подтвердиться ещё
        через десяток. Подтверждение обязательно — см. :data:`HOLD_MS`.

        Сработав один раз, детектор молчит до :meth:`reset`: имя остаётся в
        гипотезе до конца фразы, и без этого «Джарвис, включи музыку»
        срабатывало бы полсотни раз подряд.
        """
        self._recognizer.AcceptWaveform(frame.data)
        partial = json.loads(self._recognizer.PartialResult()).get("partial", "")
        heard = any(word in self._words for word in partial.split())
        self._age_ms += frame.duration * 1000

        if not heard:
            # Гипотеза отменилась — значит показалось. Копить заново.
            self._held_ms = 0.0
            if self._age_ms >= REFRESH_MS:
                # Декодер дорожает с возрастом, и конец фразы этого не лечит:
                # замер на музыке дал рост с 2.8% до 14.5% ядра за четыре
                # минуты и со сбросом по концу фразы, и без него. Лечит только
                # новый декодер — ровно то, что делал `reset` после имени.
                # Пересоздаём, только пока имени в гипотезе нет.
                self._refresh()
            return False

        self._held_ms += frame.duration * 1000
        if self._held_ms < self._hold_ms or self._fired:
            return False

        self._fired = True
        self._score = 1.0
        self._hypothesis = partial
        return True

    def reset(self) -> None:
        """Забыть накопленное после срабатывания.

        Декодер держит частичный результат до конца фразы, и без сброса имя
        осталось бы в нём ещё на секунду — то есть сработало бы несколько раз
        подряд на одном слове.
        """
        self._recognizer = self._new()
        self._score = 0.0
        self._held_ms = 0.0
        self._fired = False
        self._age_ms = 0.0

    def _refresh(self) -> None:
        """Взять новый декодер, не трогая состояние срабатывания."""
        self._recognizer = self._new()
        self._age_ms = 0.0


#: Сколько речи сверх самого слова ещё считается «сказано отдельно», мс.
#: Первая прикидка для замера: пауза вдоха и хвост детектора речи. Подбирается
#: по логу — в нём пишутся и длительность речи, и длительность слова.
ALONE_SLACK_MS = 500.0
#: Длительность слова, если декодер не отдал разметку.
WORD_MS_GUESS = 600.0


@dataclass(frozen=True, slots=True)
class Spotted:
    """Что услышал детектор слов без имени к концу фразы."""

    #: Какие слова из списка прозвучали.
    words: tuple[str, ...]
    #: Самая длинная промежуточная гипотеза фразы: «дальше» или «[unk] [unk] дальше».
    heard: str
    #: Прозвучало ли слово само по себе, без чужой речи вокруг.
    #:
    #: Считается **по длительности речи**, а не по словам декодера: на
    #: грамматике Vosk `[unk]` не пишет ни в финальном тексте, ни в
    #: промежуточных гипотезах, и «ну давай дальше рассказывай» приходит просто
    #: как «дальше» (проверено на настоящей модели 15.09.2026). Речи во фразе
    #: не больше, чем длится само слово с запасом, — значит, одно слово.
    alone: bool
    #: Уверенность декодера в самом слабом из слов. На грамматике всегда 1.0 —
    #: как порог не годится, пишется для полноты.
    confidence: float | None = None
    #: Сколько речи было во фразе, мс (по детектору речи); ``None`` — не знаем.
    speech_ms: float | None = None
    #: Сколько длятся сами слова из списка, мс (по разметке декодера).
    word_ms: float | None = None


class HotwordSpotter:
    """Слова без имени — «пауза», «дальше», «громче» — пока только для замера.

    Отдельный декодер, а не добавка слов в словарь имени: слова внутри одного
    декодера конкурируют, и «дальше» рядом с «джарвис» могло бы испортить
    распознавание самого имени. Модель при этом общая — вторая копия в память не
    грузится.

    **Решение принимается по концу фразы**, а не по частичной гипотезе, как у
    имени. Спешить некуда — музыку глушить не нужно, — а конец фразы честнее: по
    нему видно, прозвучало слово само по себе или посреди разговора.
    """

    def __init__(self, model: Any, factory: Any, words: Sequence[str], *, sample_rate: int = 16000) -> None:
        self._words = tuple(dict.fromkeys(known_words(words)))
        if not self._words:
            raise AudioError(
                f"Ни одно слово не годится для русской модели: {', '.join(words) or '(пусто)'}"
            )
        self._model = model
        self._factory = factory
        self._rate = sample_rate
        self._grammar = json.dumps([*self._words, UNKNOWN], ensure_ascii=False)
        self._recognizer = self._new()
        self._age_ms = 0.0
        #: Самая длинная промежуточная гипотеза текущей фразы и был ли в ней `[unk]`.
        self._longest = ""
        self._unknown = False
        #: Сколько речи набралось с конца прошлой фразы; ``None`` — речь не сообщают.
        self._speech_ms: float | None = None

    @classmethod
    def sharing(cls, detector: VoskWakeWord, words: Sequence[str]) -> "HotwordSpotter":
        """Собрать на модели, которую уже поднял детектор имени."""
        return cls(detector._model, detector._factory, words, sample_rate=detector._rate)

    @property
    def words(self) -> tuple[str, ...]:
        """Слушаемые слова — те, что годятся модели."""
        return self._words

    def _new(self) -> Any:
        recognizer = self._factory(self._model, float(self._rate), self._grammar)
        # Уверенность по словам — чтобы по итогам замера выбрать порог.
        set_words = getattr(recognizer, "SetWords", None)
        if callable(set_words):
            set_words(True)
        return recognizer

    def feed(self, frame: AudioFrame, *, speech: bool | None = None) -> Spotted | None:
        """Подать кадр; к концу фразы, если в ней было слово из списка, — что услышано.

        :param speech: речь ли в кадре по детектору речи. По нему считается,
            прозвучало ли слово отдельно; не сообщили — признак берётся по `[unk]`.
        """
        self._age_ms += frame.duration * 1000
        if speech is not None:
            self._speech_ms = (self._speech_ms or 0.0) + (frame.duration * 1000 if speech else 0.0)
        if not self._recognizer.AcceptWaveform(frame.data):
            partial = str(json.loads(self._recognizer.PartialResult()).get("partial", "")).strip()
            if UNKNOWN in partial.split():
                self._unknown = True
            if len(partial.split()) > len(self._longest.split()):
                self._longest = partial
            if self._age_ms >= REFRESH_MS * 2:
                # Сплошной звук без конца фразы (музыка): декодер дорожает с
                # возрастом, и лечит только новый. Слово на стыке может
                # потеряться — для замера это дешевле растущей нагрузки.
                self._recognizer = self._new()
                self._age_ms = 0.0
                self._longest, self._unknown = "", False
            return None
        final = json.loads(self._recognizer.Result())
        heard = str(final.get("text", "")).strip()
        longest, unknown, speech_ms = self._longest or heard, self._unknown, self._speech_ms
        self._longest, self._unknown = "", False
        self._speech_ms = None if speech is None else 0.0
        if self._age_ms >= REFRESH_MS:
            # Конец фразы — безопасный стык, чтобы заменить постаревший декодер.
            # Возраст считается с создания, а не с последней фразы: сброс по концу
            # фразы нагрузку не лечит (замер у REFRESH_MS).
            self._recognizer = self._new()
            self._age_ms = 0.0
        found = tuple(word for word in heard.split() if word in self._words)
        if not found:
            return None
        marked = [
            item for item in final.get("result") or ()
            if isinstance(item, dict) and item.get("word") in self._words
        ]
        confidences = [float(item.get("conf", 0.0)) for item in marked]
        spans = [
            (float(item["end"]) - float(item["start"])) * 1000
            for item in marked
            if "start" in item and "end" in item
        ]
        word_ms = sum(spans) if spans else None
        if speech_ms is not None:
            # Речи не больше, чем длятся сами слова с запасом, — значит, отдельно.
            alone = speech_ms <= (word_ms or WORD_MS_GUESS) + ALONE_SLACK_MS
        else:
            alone = not unknown and UNKNOWN not in heard.split()
        return Spotted(
            words=found,
            heard=longest,
            alone=alone,
            confidence=min(confidences) if confidences else None,
            speech_ms=speech_ms,
            word_ms=word_ms,
        )
