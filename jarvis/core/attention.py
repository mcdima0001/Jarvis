"""Когда уместно заговорить самому.

Ассистент, который замечает и сообщает, — это то, чего не хватало больше всего:
до сих пор он существовал ровно те секунды, пока к нему обращались. Но у этой
способности есть ровно одна настоящая опасность, и она не техническая.
**Ассистента, который дёргает по пустякам, выключают через день.** Поэтому
политика «когда говорить» вынесена в одно место и живёт отдельно от тех, кому
есть что сказать.

Три важности, и разница между ними в том, что случится, если промолчать:

* `LOW` — не узнает ничего страшного («тебе написали в телеграме»);
* `NORMAL` — не узнает результат того, что сам же и поручил;
* `URGENT` — не узнает о том, что просил сообщить обязательно.

**Заблокированное не выбрасывается, а придерживается.** Выбросить доклад о
поручении значит потерять работу, которую человек ждал. Придержанное
произносится при следующем разговоре — тогда, когда владелец заведомо рядом и
слушает.

**Напоминания сюда не идут, и это не упущение.** Политика — про речь, о которой
не просили. Сработавшее напоминание просили буквально, и придерживать его
значило бы нарушить обещание, ради которого его и заводили.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from jarvis.core.bus import EventBus
from jarvis.core.contracts import AnnouncementRequested
from jarvis.core.state import DEAF, Modes

logger = logging.getLogger(__name__)

#: Важности по возрастанию. Строки, а не числа: их видно в логе и в конфиге.
LOW = "low"
NORMAL = "normal"
URGENT = "urgent"

_WEIGHT = {LOW: 0, NORMAL: 1, URGENT: 2}

#: Сколько придержанных реплик помнить. Больше пяти никто не дослушает, а
#: устаревшие новости хуже, чем никакие.
MAX_HELD = 5


def weight(importance: str) -> int:
    """Числовой вес важности; неизвестное считается самым низким."""
    return _WEIGHT.get(importance, 0)


def parse_clock(value: str) -> int | None:
    """Разобрать «23:00» в минуты от полуночи. Непонятное — ``None``."""
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hours, minutes = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        return None
    return hours * 60 + minutes


def within(moment: int, start: int, end: int) -> bool:
    """Попадает ли минута суток в промежуток, возможно переходящий полночь.

    «С 23:00 до 08:00» — обычный ночной случай, и он переходит через полночь.
    Сравнение «start <= moment < end» на нём молча возвращало бы ложь всегда.
    """
    if start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


@dataclass(frozen=True, slots=True, kw_only=True)
class Held:
    """Придержанная реплика."""

    text: str
    language: str = "ru"
    importance: str = NORMAL
    at: float = 0.0


class Announcer:
    """Решает, произносить ли то, о чём не спрашивали."""

    def __init__(
        self,
        *,
        events: EventBus | None = None,
        modes: Modes | None = None,
        enabled: bool = True,
        quiet_from: str = "",
        quiet_to: str = "",
        min_gap_s: float = 60.0,
        repeat_after_s: float = 600.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._events = events
        self._modes = modes
        self._enabled = enabled
        self._quiet = (parse_clock(quiet_from), parse_clock(quiet_to))
        self._min_gap = max(0.0, min_gap_s)
        self._repeat_after = max(0.0, repeat_after_s)
        self._clock = clock or datetime.now
        self._last_spoken = 0.0
        self._said: dict[str, float] = {}
        self._held: deque[Held] = deque(maxlen=MAX_HELD)

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "attention"

    @property
    def held(self) -> tuple[Held, ...]:
        """Что придержано и ждёт ближайшего разговора."""
        return tuple(self._held)

    # --- политика ----------------------------------------------------------

    def quiet_now(self) -> bool:
        """Идёт ли сейчас время тишины."""
        start, end = self._quiet
        if start is None or end is None:
            return False
        moment = self._clock()
        return within(moment.hour * 60 + moment.minute, start, end)

    def _deaf(self) -> bool:
        """Просили ли не слушать."""
        return self._modes is not None and self._modes.active(DEAF)

    def _repeated(self, text: str, now: float) -> bool:
        """Говорили ли это же совсем недавно."""
        last = self._said.get(text)
        return last is not None and now - last < self._repeat_after

    def verdict(self, importance: str, text: str, *, now: float | None = None) -> str:
        """Что делать с репликой: ``say``, ``hold`` или ``drop``.

        Вынесено отдельно от произнесения, потому что решение тут — чистая
        функция от состояния, и проверять его надо без синтеза и без шины.
        """
        moment = now if now is not None else time.time()
        if self._repeated(text, moment):
            # Повтор одного и того же — всегда мусор, какой бы важности он ни
            # был: второй раз услышать то же самое ничего не добавляет.
            return "drop"
        if weight(importance) >= weight(URGENT):
            return "say"
        if not self._enabled:
            return "hold"
        if self._deaf():
            # Просили не слушать. Говорить в ответ на это — худшее, что можно
            # сделать: режим включают как раз чтобы не отвлекали.
            return "hold"
        if self.quiet_now():
            return "hold"
        if moment - self._last_spoken < self._min_gap:
            return "hold"
        return "say"

    # --- произнесение ------------------------------------------------------

    def offer(
        self,
        text: str,
        *,
        importance: str = NORMAL,
        language: str = "ru",
        hold: bool = True,
    ) -> str:
        """Предложить реплику. Возвращает принятое решение.

        :param hold: придерживать ли, если сказать сейчас нельзя. Обычно да:
            доклад о поручении ждали, и досказать его позже — правильно. Но
            бывает речь, уместная **только сейчас**: ироничная реплика на то,
            что человек набирает, через двадцать минут прозвучит невпопад.
            Для неё ``hold=False`` — «сказать или забыть», без очереди.
        """
        clean = text.strip()
        if not clean:
            return "drop"

        now = time.time()
        decision = self.verdict(importance, clean, now=now)
        if decision == "hold" and not hold:
            # Придержать нечего смысла: реплика привязана к моменту.
            logger.debug("Не время и держать незачем, отбросил: %s", clean)
            return "drop"
        if decision == "say":
            self._speak(clean, language, importance, now)
        elif decision == "hold":
            self._held.append(
                Held(text=clean, language=language, importance=importance, at=now)
            )
            logger.info("Придержал (%s): %s", importance, clean)
        else:
            logger.debug("Отбросил повтор: %s", clean)
        return decision

    def flush(self, *, language: str = "ru") -> int:
        """Произнести придержанное — владелец рядом и только что говорил с нами.

        :return: сколько реплик прозвучало.

        Момент выбран не случайно: человек, который сам обратился к ассистенту,
        заведомо слушает. Будить его ради накопленного было бы ровно тем, от
        чего вся эта политика и защищает.
        """
        if not self._held:
            return 0
        now = time.time()
        items = list(self._held)
        self._held.clear()
        for item in items:
            self._speak(item.text, item.language or language, item.importance, now)
        logger.info("Досказал придержанное: %d реплик(и)", len(items))
        return len(items)

    def _speak(self, text: str, language: str, importance: str, now: float) -> None:
        """Отправить реплику тому, кто её произнесёт."""
        self._last_spoken = now
        self._said[text] = now
        self._forget_old(now)
        logger.info("Говорю сам (%s): %s", importance, text)
        if self._events is not None:
            self._events.emit(
                AnnouncementRequested(source="attention", text=text, language=language)
            )

    def _forget_old(self, now: float) -> None:
        """Выбросить из памяти повторов то, что уже не считается повтором."""
        if self._repeat_after <= 0:
            self._said.clear()
            return
        stale = [text for text, at in self._said.items() if now - at >= self._repeat_after]
        for text in stale:
            self._said.pop(text, None)
