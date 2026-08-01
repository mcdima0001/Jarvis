"""Напоминания и таймеры — «часы» ассистента.

До сих пор между командами Jarvis не существовал: не было ничего, что живёт
своей жизнью и срабатывает само. «Напомни через час» выразить было нечем — и
дело не в понимании фразы, а в том, что удержать это решение, пока час идёт,
было негде.

Три вещи, которые определили устройство:

* **Скилл, а не ядро.** Ядру напоминания не нужны ни для чего: в отличие от
  режимов, которые читает голосовой конвейер, сюда не заглядывает никто.
  Фоновая задача, хранилище и вызов по имени у скиллов уже есть, поэтому
  «часы» — это ровно плагин, и ядро о них не знает.
* **Срок хранится в стенных часах, а не в монотонных.** Монотонное время
  обнуляется вместе с процессом, а напоминание обязано пережить перезапуск —
  иначе «напомни через час» умирает вместе с Jarvis, и это худший вид поломки:
  человек уже понадеялся.
* **Сработавшее напоминание не идёт прямо в синтез.** Оно уходит событием
  (`AnnouncementRequested`), а произносит его голосовой конвейер. Только он
  глушит микрофон на время речи — иначе ассистент услышит собственное
  напоминание и, попав в открытое окно ответа, выполнит его как команду.

Отложенных **действий** («через 20 минут поставь музыку на паузу») тут
намеренно нет. Это уже не часы, а планы: чтобы отложить действие, кто-то должен
выбрать инструмент и аргументы заранее, а исполнить их потом, без человека
рядом. У планов свои правила безопасности, и делать их надо отдельно.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta

from jarvis.core.contracts import AnnouncementRequested, ToolResult, parse_number
from jarvis.core.errors import MemoryError_
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool
from jarvis.core.tts.normalize import plural_form

#: Раздел памяти. Должен быть объявлен в `memory.documents`; нет — скилл
#: работает, но переживать перезапуск напоминания перестают.
SECTION = "reminders"

#: Как часто заглядывать в список. Напоминания живут в минутах, поэтому десяти
#: секунд с запасом хватает, а стоит такая проверка ровно ничего.
CHECK_SECONDS = 10.0

#: Сколько ждать до первой проверки. Ассистент при запуске здоровается, и
#: напоминание, пропущенное за ночь, не должно перебивать приветствие.
FIRST_CHECK_S = 20.0

#: Насколько опоздавшее напоминание ещё стоит произносить. Просроченное на
#: пятнадцать часов — это уже не напоминание, а недоумение среди ночи.
LATE_AFTER_H = 12.0

#: С какого опоздания стоит извиниться и назвать время. Полминуты набегает
#: интервалом проверки — про такое говорить нечего.
LATE_MENTION_S = 90.0

#: Сколько напоминаний держать. Предел не от жадности: список зачитывается
#: вслух, и полсотни — это уже не список, а наказание.
MAX_ITEMS = 50

#: Дальше какого срока не заводим. Год — это не напоминание, это календарь.
MAX_AHEAD_DAYS = 366

logger = logging.getLogger(__name__)

_HOURS = ("час", "часа", "часов")
_MINUTES = ("минуту", "минуты", "минут")

#: Слова, которые сказаны человеку, а не про дело: «напомни **мне**, **что**…».
#: В тексте напоминания им места нет — их произнесут обратно и будет каша.
_FILLER = (
    "мне", "меня", "пожалуйста", "плиз", "что", "чтобы", "чтоб",
    "о", "том", "про", "то", "please", "me", "to", "that", "about",
)

#: Числительные, которых нет ни в цифрах, ни в общем словаре чисел.
_HALVES = {"полтора": 1.5, "полторы": 1.5, "пол": 0.5}

#: Единица времени без «через»: «на минуту», «5 минут», «полторы минуты».
_DURATION = re.compile(
    r"(?:(?P<count>\d+(?:[.,]\d+)?|[а-яё]+(?:\s+[а-яё]+)?)\s*)?"
    r"(?P<unit>секунд\w*|сек\b|минут\w*|мин\b|час(?:а|ов|у)?\b|"
    r"seconds?|secs?|minutes?|mins?|hours?)\b",
    re.IGNORECASE,
)

#: «через полчаса» и «через полтора часа» — обычные слова, но числом их не
#: записать, а говорят их постоянно.
_SPECIALS: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"\bчерез\s+полтора\s+час\w*", re.IGNORECASE), 90.0),
    (re.compile(r"\bчерез\s+полчаса\b", re.IGNORECASE), 30.0),
    (re.compile(r"\bчерез\s+час\b", re.IGNORECASE), 60.0),
    (re.compile(r"\bin\s+half\s+an\s+hour\b", re.IGNORECASE), 30.0),
)

#: «через 20 минут», «через двадцать минут», «через 2 часа».
_RELATIVE = re.compile(
    r"\bчерез\s+(?:(?P<count>\d+(?:[.,]\d+)?|[а-яё]+(?:\s+[а-яё]+){0,2})\s+)?"
    r"(?P<unit>секунд\w*|сек\b|минут\w*|мин\b|час(?:а|ов|у)?\b|ч\b)",
    re.IGNORECASE,
)

_RELATIVE_EN = re.compile(
    r"\bin\s+(?:(?P<count>\d+(?:[.,]\d+)?)\s+)?"
    r"(?P<unit>seconds?|secs?|minutes?|mins?|hours?)\b",
    re.IGNORECASE,
)

#: «в 18:30», «завтра в 9 утра», «в 21 час».
_ABSOLUTE = re.compile(
    r"\b(?:(?P<day>сегодня|завтра|послезавтра)\s+)?"
    r"(?:в|к)\s+(?P<hour>\d{1,2})(?:[:.](?P<minute>\d{2}))?"
    r"(?:\s*час\w*)?"
    r"(?:\s*(?P<part>утра|дня|вечера|ночи))?",
    re.IGNORECASE,
)

_ABSOLUTE_EN = re.compile(
    r"\b(?:(?P<day>today|tomorrow)\s+)?at\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?"
    r"\s*(?P<part>am|pm)?\b",
    re.IGNORECASE,
)

_NOON = re.compile(r"\b(?:(?P<day>сегодня|завтра)\s+)?в\s+(?P<what>полдень|полночь)\b",
                   re.IGNORECASE)

_DAY_SHIFT = {"сегодня": 0, "today": 0, "завтра": 1, "tomorrow": 1, "послезавтра": 2}


def _seconds_in(unit: str) -> float:
    """Сколько секунд в названной единице."""
    unit = unit.lower()
    if unit.startswith(("сек", "sec")):
        return 1.0
    if unit.startswith(("мин", "min")):
        return 60.0
    return 3600.0


def _count_of(raw: str | None) -> float | None:
    """Сколько единиц названо. Без числа — одна: «на минуту» это одна минута."""
    if raw is None:
        return 1.0
    text = " ".join(raw.split()).lower()
    if not text:
        return 1.0
    for word, value in _HALVES.items():
        if text.endswith(word):
            return value
    return parse_number(text)


def parse_duration(text: str) -> float | None:
    """Сколько минут названо: «минуту», «5 минут», «полторы минуты», «полчаса».

    Отдельно от `parse_when`, потому что вопрос другой: там «когда», здесь
    «сколько». И отвечать «не понял» тут так же обязательно: таймер, поставленный
    не на то время, хуже непоставленного — про него-то человек знает.
    """
    text = " ".join(text.split())
    if not text:
        return None
    if re.search(r"\bполчаса\b", text, re.IGNORECASE):
        return 30.0
    found = _DURATION.search(text)
    if not found:
        return None
    count = _count_of(found.group("count"))
    if count is None or count <= 0:
        return None
    minutes = count * _seconds_in(found.group("unit")) / 60.0
    return minutes if 0 < minutes <= MAX_AHEAD_DAYS * 24 * 60 else None


def _length_phrase(minutes: float) -> str:
    """Назвать длительность так, как её произносят.

    Дробные минуты в речи не звучат: «0.5 минуты» это «30 секунд», а «1.5» —
    «полторы минуты». Читать вслух десятичную точку синтез всё равно не умеет.
    """
    if minutes < 1:
        seconds = max(1, round(minutes * 60))
        return f"{seconds} {plural_form(seconds, ('секунду', 'секунды', 'секунд'))}"
    if abs(minutes - 1.5) < 0.01:
        return "полторы минуты"
    whole = int(round(minutes))
    if whole % 60 == 0 and whole >= 60:
        hours = whole // 60
        return f"{hours} {plural_form(hours, _HOURS)}"
    return f"{whole} {plural_form(whole, _MINUTES)}"


def _shift(day: str | None) -> int:
    """На сколько суток вперёд указывает слово «завтра»."""
    return _DAY_SHIFT.get((day or "").lower(), 0)


def _hour_of_day(hour: int, part: str | None) -> int:
    """Перевести «9 вечера» в 21, а «2 ночи» оставить двойкой."""
    mark = (part or "").lower()
    if not mark:
        return hour
    if mark in ("утра", "am"):
        return 0 if hour == 12 and mark == "am" else hour
    if mark == "ночи":
        # «в 11 ночи» — это 23, а «в 2 ночи» — это 2. Граница проходит там, где
        # ночь перестаёт быть продолжением вечера.
        return hour + 12 if hour >= 8 else hour
    # день, вечер, pm
    return hour + 12 if hour < 12 else hour


def strip_filler(text: str) -> str:
    """Убрать служебные слова по краям текста напоминания."""
    words = [word for word in re.split(r"\s+", text.strip(" ,.;:!?-—")) if word]
    while words and words[0].strip(" ,.;:").lower() in _FILLER:
        words.pop(0)
    while words and words[-1].strip(" ,.;:").lower() in _FILLER:
        words.pop()
    return " ".join(words).strip(" ,.;:!?-—")


def parse_when(request: str, now: datetime | None = None) -> tuple[datetime | None, str]:
    """Выделить из просьбы время и текст напоминания.

    Разбор идёт кодом, а не моделью, и это не экономия ради экономии: «через
    20 минут» — не та задача, где нужна догадка, а ошибка тут дорогая. Если
    формулировку разобрать не удалось, возвращается ``None`` — и ассистент
    честно переспрашивает. Наугад ставить время нельзя: человек уже
    понадеялся, а узнает об ошибке через час.

    :param request: то, что осталось от фразы после «напомни».
    :param now: точка отсчёта; по умолчанию — текущее время.
    :return: пара «когда» и «что», где «когда» может быть ``None``.
    """
    moment = now or datetime.now()
    text = " ".join(request.split())
    if not text:
        return None, ""

    for pattern, minutes in _SPECIALS:
        found = pattern.search(text)
        if found:
            rest = text[: found.start()] + " " + text[found.end() :]
            return moment + timedelta(minutes=minutes), strip_filler(rest)

    for pattern in (_RELATIVE, _RELATIVE_EN):
        found = pattern.search(text)
        if not found:
            continue
        raw = found.group("count")
        # Без числа — значит «через минуту», «через час»: одна единица.
        count = 1.0 if raw is None else parse_number(raw)
        if count is None or count <= 0:
            continue
        seconds = count * _seconds_in(found.group("unit"))
        if seconds > MAX_AHEAD_DAYS * 86400:
            return None, text
        rest = text[: found.start()] + " " + text[found.end() :]
        return moment + timedelta(seconds=seconds), strip_filler(rest)

    found = _NOON.search(text)
    if found:
        hour = 12 if found.group("what").lower() == "полдень" else 0
        return _at_clock(moment, hour, 0, _shift(found.group("day")), forced=False), \
            strip_filler(text[: found.start()] + " " + text[found.end() :])

    for pattern in (_ABSOLUTE, _ABSOLUTE_EN):
        found = pattern.search(text)
        if not found:
            continue
        said = int(found.group("hour"))
        part = found.group("part")
        hour = _hour_of_day(said, part)
        minute = int(found.group("minute") or 0)
        if hour > 23 or minute > 59:
            continue
        rest = text[: found.start()] + " " + text[found.end() :]
        day = found.group("day")
        when = _at_clock(moment, hour, minute, _shift(day), forced=bool(day))
        if not part and not day:
            when = _prefer_evening(when, moment, said, minute)
        return when, strip_filler(rest)

    return None, strip_filler(text)


def _at_clock(now: datetime, hour: int, minute: int, shift: int, *, forced: bool) -> datetime:
    """Собрать момент по часам и минутам.

    Названное время без даты означает ближайшее такое: сказанное в семь вечера
    «в девять» — это сегодня, а сказанное в десять — уже завтра. Догадка тут
    единственно разумная, и ошибиться в другую сторону хуже: напоминание,
    просроченное на сутки, не сработает вовсе.
    """
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    when += timedelta(days=shift)
    if when <= now and not forced:
        when += timedelta(days=1)
    return when


def _prefer_evening(when: datetime, now: datetime, hour: int, minute: int) -> datetime:
    """«В восемь», сказанное днём, означает вечер, а не завтрашнее утро.

    Догадка нужна, потому что молчать нельзя: одно из двух пониманий всё равно
    придётся выбрать. Выбираем то, где ошибка дешевле. Промахнуться в вечер —
    это напоминание раньше нужного, его переставят; промахнуться в завтрашнее
    утро — это напоминание на двенадцать часов позже, то есть бесполезное.

    Опасности в догадке нет: понятое время ассистент проговаривает в ответ, и
    расхождение слышно сразу. Явное «утра» и «завтра» её отключают.
    """
    # Утро ещё впереди — значит его и имели в виду: «в 11», сказанное в
    # девять, это одиннадцать, а не двадцать три.
    if not 1 <= hour <= 11 or when.date() == now.date():
        return when
    evening = now.replace(hour=hour + 12, minute=minute, second=0, microsecond=0)
    return evening if evening > now else when


def clock_phrase(moment: datetime, language: str = "ru") -> str:
    """Назвать время так, чтобы это можно было произнести.

    «15:40» синтез читает как «пятнадцать двоеточие сорок», поэтому часы и
    минуты пишутся словами-единицами: «15 часов 40 минут». Числительные с ними
    согласует уже слой синтеза.
    """
    if language == "en":
        return moment.strftime("%H:%M")
    hours = f"{moment.hour} {plural_form(moment.hour, _HOURS)}"
    if not moment.minute:
        return hours
    return f"{hours} {moment.minute} {plural_form(moment.minute, _MINUTES)}"


def when_phrase(moment: datetime, now: datetime, language: str = "ru") -> str:
    """Сказать, когда это будет: близкое — сроком, далёкое — временем.

    «Напомню через пять минут» полезнее, чем «напомню в пятнадцать сорок
    пять», а через сутки — ровно наоборот.
    """
    left = (moment - now).total_seconds()
    if left < 3600:
        minutes = max(1, round(left / 60))
        if language == "en":
            return f"in {minutes} minutes"
        return f"через {minutes} {plural_form(minutes, _MINUTES)}"

    clock = clock_phrase(moment, language)
    if moment.date() == now.date():
        return clock if language == "en" else f"в {clock}"
    if moment.date() == (now + timedelta(days=1)).date():
        return f"tomorrow at {clock}" if language == "en" else f"завтра в {clock}"
    day = moment.strftime("%d.%m")
    return f"on {day} at {clock}" if language == "en" else f"{day} в {clock}"


class RemindersSkill(Skill):
    """Напоминания и таймеры, переживающие перезапуск."""

    meta = SkillMeta(
        name="reminders",
        version="0.1.0",
        description="Напоминания и таймеры: «напомни через час», «таймер на 10 минут»",
    )

    def __init__(self) -> None:
        super().__init__()
        self._items: list[dict] = []
        self._next_id = 1
        #: Получилось ли открыть раздел памяти. Не получилось — работаем, но
        #: только до перезапуска, и об этом сказано в логе один раз.
        self._persist = True
        # Значения из конфига приезжают в on_setup; здесь — чтобы объект был
        # целым с рождения и не падал, если до него доберутся раньше.
        self._check_s = CHECK_SECONDS
        self._late_after = LATE_AFTER_H * 3600
        self._limit = MAX_ITEMS

    async def on_setup(self) -> None:
        """Прочитать сохранённое и завести проверку по расписанию."""
        self._check_s = float(self.context.setting("check_seconds", CHECK_SECONDS))
        self._late_after = float(self.context.setting("late_after_h", LATE_AFTER_H)) * 3600
        self._limit = int(self.context.setting("max_items", MAX_ITEMS))
        await self._load()

    async def on_start(self) -> None:
        """Запустить фоновую проверку. Через scope — чтобы она умерла со скиллом."""
        self.context.scope.spawn(self._watch(), name="reminders-watch")

    async def health(self) -> HealthStatus:
        """Сколько напоминаний ждёт своего часа."""
        pending = len(self._items)
        note = f"напоминаний: {pending}"
        if not self._persist:
            note += "; память недоступна — перезапуск их не переживёт"
        return HealthStatus(ok=True, detail=note)

    # --- хранение ----------------------------------------------------------

    async def _load(self) -> None:
        """Поднять список с диска."""
        try:
            data = await self.context.memory.documents.read(SECTION)
        except MemoryError_ as exc:
            # Раздел не объявлен в конфиге. Это не повод не работать: терять
            # напоминания при перезапуске плохо, а не иметь их вовсе — хуже.
            self._persist = False
            self.log.warning(
                "Напоминания не сохраняются: %s. Добавь «%s» в memory.documents", exc, SECTION
            )
            return

        items = data.get("items")
        self._items = [dict(item) for item in items] if isinstance(items, list) else []
        self._next_id = int(data.get("next_id", 1)) or 1
        if self._items:
            self.log.info("Загружено напоминаний: %d", len(self._items))

    async def _save(self) -> None:
        """Записать список на диск. Тихо пропускаем, если раздела нет."""
        if not self._persist:
            return
        await self.context.memory.documents.update(
            SECTION, {"items": self._items, "next_id": self._next_id}
        )

    def _add(self, *, at: float, text: str, kind: str, language: str, minutes: float) -> dict:
        """Положить напоминание в список по времени срабатывания."""
        item = {
            "id": self._next_id,
            "at": at,
            "text": text,
            "kind": kind,
            "language": language,
            "minutes": round(minutes, 2),
        }
        self._next_id += 1
        self._items.append(item)
        self._items.sort(key=lambda entry: entry["at"])
        return item

    # --- срабатывание ------------------------------------------------------

    async def _watch(self) -> None:
        """Раз в несколько секунд смотреть, не пора ли.

        Именно опрос, а не сон до ближайшего срока: список меняется из команд,
        и «поспать до 18:00» пришлось бы прерывать и пересчитывать на каждую
        правку. Проверка стоит доли микросекунды, а кода в ней втрое меньше.
        """
        await asyncio.sleep(min(FIRST_CHECK_S, max(1.0, self._check_s)))
        while True:
            try:
                await self._fire_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Сбой одной проверки не должен уносить с собой все будущие.
                self.log.exception("Проверка напоминаний сорвалась")
            await asyncio.sleep(self._check_s)

    async def _fire_due(self) -> None:
        """Произнести всё, чему пришло время, и убрать из списка."""
        now = time.time()
        due = [item for item in self._items if float(item["at"]) <= now]
        if not due:
            return

        self._items = [item for item in self._items if float(item["at"]) > now]
        await self._save()

        for item in due:
            late = now - float(item["at"])
            if late > self._late_after:
                # Напоминание, просроченное на полсуток, уже никому не помогает,
                # а среди ночи ещё и пугает. В лог — обязательно: молча терять
                # обещанное нельзя.
                self.log.warning(
                    "Пропущено напоминание %r: опоздало на %.0f ч",
                    item.get("text", ""),
                    late / 3600,
                )
                continue
            text = self._announcement(item, late=late)
            self.log.info("Напоминание сработало: %s", text)
            self.events.emit(
                AnnouncementRequested(
                    source=self.meta.name,
                    text=text,
                    language=str(item.get("language", "ru")),
                )
            )

    def _announcement(self, item: dict, *, late: float) -> str:
        """Собрать фразу, которую произнесут вслух."""
        language = str(item.get("language", "ru"))
        body = str(item.get("text", "")).strip()
        moment = datetime.fromtimestamp(float(item["at"]))

        if item.get("kind") == "timer":
            spoken = _length_phrase(float(item.get("minutes", 0)))
            if language == "en":
                return f"Timer for {spoken}. Time's up."
            return f"Таймер на {spoken}. Время вышло."

        # Опоздание называется временем, а не «извини»: важно, на когда оно
        # было, — по этому человек и поймёт, о чём речь.
        prefix = "Напоминание"
        if late > LATE_MENTION_S:
            prefix = (
                f"Напоминание было на {clock_phrase(moment, language)}"
                if language == "ru"
                else f"Reminder was set for {clock_phrase(moment, language)}"
            )
        elif language == "en":
            prefix = "Reminder"
        if not body:
            return f"{prefix}." if language == "ru" else f"{prefix}."
        return f"{prefix}: {body}."

    # --- инструменты -------------------------------------------------------

    @tool(
        phrases=[
            "напомни {request}", "напомни мне {request}", "напоминание {request}",
            "поставь напоминание {request}", "поставь напоминание на {request}",
            "remind me {request}", "reminder {request}",
        ]
    )
    async def remind(self, request: str = "", language: str = "ru") -> ToolResult:
        """Напомнить о чём-нибудь в названное время.

        Понимает «через 20 минут», «через полчаса», «в 18:30», «завтра в 9
        утра» — и время может стоять где угодно во фразе: «напомни через час
        позвонить маме» и «напомни позвонить маме через час» одинаковы.

        :param request: когда и о чём напомнить, одной фразой.
        :param language: на каком языке произнести напоминание.
        """
        if not request.strip():
            return ToolResult.failure(
                "не сказано, о чём и когда напомнить",
                speech={
                    "ru": ("О чём напомнить и когда?", "Что напомнить и во сколько?"),
                    "en": ("What should I remind you about, and when?",),
                },
            )

        now = datetime.now()
        moment, body = parse_when(request, now)
        if moment is None:
            # Наугад ставить время нельзя: человек понадеется и узнает об
            # ошибке ровно тогда, когда напоминание не сработает.
            return ToolResult.failure(
                f"не разобрал время в {request!r}",
                speech={
                    "ru": ("Не понял, на когда. Скажи, например, «через двадцать минут» "
                           "или «в восемнадцать тридцать».",),
                    "en": ("I didn't catch the time. Try “in twenty minutes” or “at 18:30”.",),
                },
            )
        if len(self._items) >= self._limit:
            return ToolResult.failure(
                f"напоминаний уже {len(self._items)}",
                speech={
                    "ru": (f"У меня уже {len(self._items)} напоминаний. Отмени лишние.",),
                    "en": (f"I already have {len(self._items)} reminders. Cancel some first.",),
                },
            )

        left = (moment - now).total_seconds()
        item = self._add(
            at=moment.timestamp(),
            text=body,
            kind="reminder",
            language=language,
            minutes=left / 60,
        )
        await self._save()
        self.log.info("Напоминание #%d на %s: %r", item["id"], moment.strftime("%d.%m %H:%M"), body)

        when = when_phrase(moment, now, language)
        when_en = when_phrase(moment, now, "en")
        about = f": {body}" if body else ""
        return ToolResult.success(
            {"id": item["id"], "at": item["at"], "text": body},
            speech={
                "ru": (f"Напомню {when}{about}.", f"Хорошо, {when}{about}."),
                "en": (f"I'll remind you {when_en}{about}.",),
            },
        )

    @tool(
        # Подстановка стоит **последней** и забирает хвост целиком: «на минуту»,
        # «на 5 минут», «на полторы минуты». Раньше в шаблоне было число, и
        # окончание приходилось перечислять по три раза на глагол, а «на минуту»
        # (без числа вовсе) не подходило ни под одно написание.
        phrases=[
            "поставь таймер", "заведи таймер", "таймер",
            "поставь таймер на {request}", "поставь таймер {request}",
            "таймер на {request}", "засеки {request}", "заведи таймер на {request}",
            "timer for {request}", "set a timer for {request}",
        ],
        routable=False,
    )
    async def timer(self, request: str = "", language: str = "ru") -> ToolResult:
        """Засечь время и сказать, когда оно выйдет.

        :param request: сколько времени: «минуту», «5 минут», «полторы минуты».
        :param language: на каком языке объявить.
        """
        minutes = parse_duration(request)
        if minutes is None:
            # Умолчание тут было бы догадкой, а догадка про время — худший вид
            # ошибки: живой случай 01.08.2026, «поставь таймер на минуту»
            # превратилось в пять. Фраза дошла нечётким совпадением, подстановка
            # осталась пустой, и умолчание молча выиграло.
            return ToolResult.failure(
                f"не разобрал длительность в {request!r}",
                speech={
                    "ru": ("На сколько ставить таймер?", "Сколько засечь?"),
                    "en": ("How long should the timer be?",),
                },
            )
        now = datetime.now()
        moment = now + timedelta(minutes=minutes)
        item = self._add(
            at=moment.timestamp(), text="", kind="timer", language=language, minutes=minutes
        )
        await self._save()

        spoken = _length_phrase(minutes)
        self.log.info("Таймер #%d на %.2f мин (из %r)", item["id"], minutes, request)
        return ToolResult.success(
            {"id": item["id"], "at": item["at"], "minutes": minutes},
            speech={
                "ru": (f"Таймер на {spoken}.", f"Засёк {spoken}."),
                "en": (f"Timer set for {spoken}.",),
            },
        )

    @tool(
        phrases=["какие напоминания", "мои напоминания", "что запланировано",
                 "какие у меня напоминания", "что мне напомнить", "какие таймеры",
                 "what reminders do i have", "my reminders"],
        routable=False,
    )
    async def pending(self, language: str = "ru") -> ToolResult:
        """Перечислить напоминания, которые ещё ждут."""
        if not self._items:
            return ToolResult.success(
                [],
                speech={
                    "ru": ("Напоминаний нет.", "Ничего не запланировано."),
                    "en": ("No reminders.",),
                },
            )

        now = datetime.now()
        lines: list[str] = []
        for item in self._items:
            moment = datetime.fromtimestamp(float(item["at"]))
            when = when_phrase(moment, now, language)
            body = str(item.get("text", "")).strip()
            lines.append(f"{when} — {body}" if body else f"таймер {when}")

        listed = "; ".join(lines)
        return ToolResult.success(
            [{"id": item["id"], "at": item["at"], "text": item.get("text", "")}
             for item in self._items],
            speech={
                "ru": f"Напоминаний {len(self._items)}: {listed}.",
                "en": f"{len(self._items)} reminders: {listed}.",
            },
        )

    @tool(
        phrases=["отмени напоминание", "отмени напоминания", "отмени все напоминания",
                 "убери напоминание", "отмени таймер", "убери таймер",
                 "отмени напоминание {which}", "отмени напоминание про {which}",
                 "cancel the reminder", "cancel reminders"],
        routable=False,
    )
    async def cancel(self, which: str = "") -> ToolResult:
        """Отменить напоминание.

        :param which: часть текста напоминания; пусто — отменить все.
        """
        if not self._items:
            return ToolResult.success(
                [],
                speech={
                    "ru": ("Нечего отменять — напоминаний нет.",),
                    "en": ("Nothing to cancel — there are no reminders.",),
                },
            )

        needle = strip_filler(which).lower()
        if not needle:
            count = len(self._items)
            self._items = []
            await self._save()
            return ToolResult.success(
                {"cancelled": count},
                speech={
                    "ru": (f"Отменил, было {count}.", "Отменил все напоминания."),
                    "en": (f"Cancelled all {count}.",),
                },
            )

        gone = [item for item in self._items if needle in str(item.get("text", "")).lower()]
        if not gone:
            return ToolResult.failure(
                f"нет напоминания про {which!r}",
                speech={
                    "ru": (f"Не нашёл напоминания про {which}.",),
                    "en": (f"I found no reminder about {which}.",),
                },
            )
        self._items = [item for item in self._items if item not in gone]
        await self._save()
        listed = ", ".join(str(item.get("text", "")) for item in gone if item.get("text"))
        return ToolResult.success(
            {"cancelled": len(gone)},
            speech={
                "ru": (f"Отменил: {listed}." if listed else "Отменил.",),
                "en": (f"Cancelled: {listed}." if listed else "Cancelled.",),
            },
        )
