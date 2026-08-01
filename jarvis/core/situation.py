"""Ситуация — то, что происходит прямо сейчас, в двух-трёх предложениях.

Разбирая фразу, модель до сих пор видела ровно две вещи: каталог инструментов
и саму фразу. Ни который час, ни что играет, ни в каком ассистент режиме, ни
что было сказано минуту назад. Из-за этого она не могла рассуждать так, как
рассуждает человек: «просят тише — значит что-то играет — значит речь про
музыку, а не про громкость системы».

Стоит это почти ничего. Каталог инструментов уезжает в модель на каждой
неузнанной фразе и весит около 2400 токенов; ситуация добавляет к нему сотню.

Главное правило: **сюда попадает только то, что уже под рукой**. Ни одного
похода в браузер, к звуковым сессиям или в сеть ради этой строки — иначе к
каждой команде добавится круг ожидания, и ради подсказки сломается то, что
работает. Отсюда и устройство: факты **приносят** те, кто их и так узнал по
дороге (`note`), а не собираются опросом.

Объём ограничен по построению, а не обрезкой: не больше `MAX_NOTES` фактов,
каждый не длиннее `MAX_NOTE`. Строка, которую нечем наполнить, получается
пустой — и в подсказку не идёт вовсе.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from .state import Modes

#: Сколько фактов держать. Больше пяти — это уже не «ситуация», а отчёт.
MAX_NOTES = 5
#: Предел длины одного факта. Название трека сюда влезает, пересказ страницы нет.
MAX_NOTE = 80

_WEEKDAYS = {
    "ru": ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
}

_MONTHS_RU = ("января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")


def now_line(language: str = "ru") -> str:
    """Строка с текущей датой и временем.

    Модель не знает, какой сегодня день: её знания заканчиваются на дате
    обучения, а часов у неё нет. Без этой строки на «какой сегодня день» она
    честно выдумывает.
    """
    code = language if language in _WEEKDAYS else "ru"
    now = datetime.now().astimezone()
    weekday = _WEEKDAYS[code][now.weekday()]
    if code == "ru":
        return (
            f"Сейчас {now.day} {_MONTHS_RU[now.month - 1]} {now.year} года, "
            f"{weekday}, {now:%H:%M}."
        )
    return f"Current date and time: {weekday}, {now:%d %B %Y, %H:%M}."


@dataclass(frozen=True, slots=True)
class Note:
    """Один факт о происходящем: подпись, значение и срок годности."""

    key: str
    value: str
    #: Монотонные секунды, после которых факт устарел; 0 — не устаревает.
    until: float = 0.0

    def stale(self, now: float) -> bool:
        """Пора ли забыть."""
        return bool(self.until) and now >= self.until


@dataclass(frozen=True, slots=True)
class LastCommand:
    """Что просили в прошлый раз и чем это кончилось."""

    text: str
    tool: str
    ok: bool


class Situation:
    """Сборщик обстановки для подсказки модели.

    Срок годности у факта обязателен по смыслу, хотя и не по типу: «открыта
    Яндекс Музыка» верно десять минут и вредно через три часа — модель поверит
    и уведёт команду на сайт, закрытый давным-давно. Поэтому у всех, кто зовёт
    `note`, стоит осмысленный срок, а бессрочными остаются только вещи, которые
    и правда не портятся.
    """

    def __init__(self, *, modes: Modes | None = None) -> None:
        self._modes = modes or Modes()
        self._notes: dict[str, Note] = {}
        self._last: LastCommand | None = None

    @property
    def modes(self) -> Modes:
        """Режимы, которые попадут в описание."""
        return self._modes

    def note(self, key: str, value: str, *, minutes: float = 0.0) -> None:
        """Сообщить факт о происходящем.

        :param key: подпись — «открыт сайт», «играет». По ней факт и заменяется.
        :param value: значение; длиннее `MAX_NOTE` обрезается.
        :param minutes: сколько факт остаётся верным; 0 — не портится.
        """
        text = " ".join(str(value).split())[:MAX_NOTE]
        if not text:
            self._notes.pop(key, None)
            return
        until = time.monotonic() + minutes * 60 if minutes > 0 else 0.0
        # Переложить в конец, а не обновить на месте: вытесняем самое старое,
        # а свежий факт старым быть не должен.
        self._notes.pop(key, None)
        self._notes[key] = Note(key=key, value=text, until=until)
        while len(self._notes) > MAX_NOTES:
            self._notes.pop(next(iter(self._notes)))

    def forget(self, key: str) -> None:
        """Убрать факт досрочно."""
        self._notes.pop(key, None)

    def command(self, text: str, *, tool: str = "", ok: bool = True) -> None:
        """Запомнить последнюю разобранную команду.

        Нужна для просьб, у которых нет своего смысла без предыдущей: «повтори»,
        «отмени», «а теперь погромче».
        """
        self._last = LastCommand(text=" ".join(text.split())[:MAX_NOTE], tool=tool, ok=ok)

    @property
    def last(self) -> LastCommand | None:
        """Последняя команда, если она была."""
        return self._last

    def notes(self) -> tuple[Note, ...]:
        """Живые факты; протухшие отсеиваются по дороге."""
        now = time.monotonic()
        for key in [key for key, note in self._notes.items() if note.stale(now)]:
            del self._notes[key]
        return tuple(self._notes.values())

    def describe(self, language: str = "ru") -> str:
        """Собрать обстановку в текст для системной подсказки.

        Пустых разделов не бывает: чего нет, то и не упоминается. Строка «режимы:
        нет» занимала бы место и ничего не сообщала.
        """
        code = language if language in _WEEKDAYS else "ru"
        parts = [now_line(code)]

        modes = self._modes.describe(code)
        if modes:
            parts.append(("Режимы: " if code == "ru" else "Modes: ") + modes + ".")

        for note in self.notes():
            parts.append(f"{note.key}: {note.value}.")

        if self._last is not None:
            done = (
                ("выполнена" if self._last.ok else "не удалась")
                if code == "ru"
                else ("done" if self._last.ok else "failed")
            )
            label = "Прошлая команда" if code == "ru" else "Previous command"
            tool = f" → {self._last.tool}" if self._last.tool else ""
            parts.append(f"{label}: «{self._last.text}»{tool}, {done}.")

        return " ".join(parts)
