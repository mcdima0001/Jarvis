"""Когда уместно заговорить самому.

У способности замечать и сообщать ровно одна настоящая опасность, и она не
техническая: ассистента, который дёргает по пустякам, выключают через день.
Поэтому здесь проверяется не «умеет говорить», а **умеет промолчать** — и при
этом ничего не терять.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from jarvis.core.attention import (
    LOW,
    NORMAL,
    URGENT,
    Announcer,
    parse_clock,
    within,
)
from jarvis.core.bus import LocalEventBus
from jarvis.core.contracts import AnnouncementRequested
from jarvis.core.state import DEAF, Modes


def _at(hour: int, minute: int = 0):
    """Часы, показывающие заданное время суток."""
    return lambda: datetime(2026, 9, 11, hour, minute)


# --- время тишины -----------------------------------------------------------


def test_clock_is_parsed_and_nonsense_is_refused() -> None:
    """«23:30» — время, «четверть восьмого» и «25:00» — нет."""
    assert parse_clock("23:30") == 23 * 60 + 30
    assert parse_clock("00:00") == 0
    assert parse_clock("25:00") is None
    assert parse_clock("восемь") is None
    assert parse_clock("8") is None


def test_night_interval_crosses_midnight() -> None:
    """«С 23:00 до 08:00» переходит через полночь, и это обычный случай.

    Наивное «start <= moment < end» на нём молча возвращало бы ложь всегда, то
    есть тишины не было бы вовсе.
    """
    night = (23 * 60, 8 * 60)
    assert within(23 * 60 + 30, *night)
    assert within(3 * 60, *night)
    assert not within(12 * 60, *night)
    # Пустой промежуток — не тишина, а её отсутствие.
    assert not within(5 * 60, 60, 60)


def test_quiet_hours_hold_the_ordinary_and_pass_the_urgent() -> None:
    """Ночью говорится только срочное, остальное ждёт утра."""
    night = Announcer(quiet_from="23:00", quiet_to="08:00", clock=_at(2))

    assert night.offer("тебе написали", importance=LOW) == "hold"
    assert night.offer("пожар", importance=URGENT) == "say"


def test_daytime_lets_the_ordinary_through() -> None:
    """Днём обычная новость проходит."""
    day = Announcer(quiet_from="23:00", quiet_to="08:00", clock=_at(14))
    assert day.offer("тебе написали", importance=LOW) == "say"


# --- режимы и вежливость ----------------------------------------------------


def test_deaf_mode_holds_everything_but_the_urgent() -> None:
    """Просили не слушать — значит и не говорим.

    Режим включают ровно чтобы не отвлекали; заговорить в ответ на это — худшее
    из возможного.
    """
    modes = Modes()
    modes.on(DEAF, minutes=30)
    quiet = Announcer(modes=modes)

    assert quiet.offer("тебе написали", importance=LOW) == "hold"
    assert quiet.offer("напоминание", importance=URGENT) == "say"


def test_switch_off_holds_instead_of_dropping() -> None:
    """Выключенная политика придерживает, а не выбрасывает.

    Выбросить доклад о поручении значит потерять работу, которую человек ждал.
    """
    off = Announcer(enabled=False)
    assert off.offer("готово", importance=NORMAL) == "hold"
    assert [item.text for item in off.held] == ["готово"]


def test_second_announcement_waits_out_the_gap() -> None:
    """Подряд не тараторим: между репликами без вопроса есть пауза."""
    talker = Announcer(min_gap_s=60.0)

    assert talker.offer("первое", importance=LOW) == "say"
    assert talker.offer("второе", importance=LOW) == "hold"


def test_hold_false_drops_instead_of_queuing() -> None:
    """Реплика, уместная только сейчас, не откладывается на потом.

    Ироничная реакция на набранное через двадцать минут прозвучит невпопад.
    Поэтому `hold=False` превращает «придержать» в «забыть», и в очередь ничего
    не попадает.
    """
    talker = Announcer(min_gap_s=60.0)

    assert talker.offer("первое", importance=LOW) == "say"
    assert talker.offer("шутка на потом", importance=LOW, hold=False) == "drop"
    assert talker.held == ()


def test_urgent_ignores_the_gap() -> None:
    """Срочное паузы не ждёт."""
    talker = Announcer(min_gap_s=60.0)
    talker.offer("первое", importance=LOW)
    assert talker.offer("второе", importance=URGENT) == "say"


def test_same_text_twice_is_dropped_not_held() -> None:
    """Повтор — всегда мусор, какой бы важности он ни был.

    Второй раз услышать то же самое ничего не добавляет, а придержать его
    значит произнести это дважды при следующем разговоре.
    """
    talker = Announcer(min_gap_s=0.0, repeat_after_s=600.0)

    assert talker.offer("тебе написала мама", importance=URGENT) == "say"
    assert talker.offer("тебе написала мама", importance=URGENT) == "drop"
    assert not talker.held


def test_repeat_is_allowed_again_after_a_while() -> None:
    """Через положенное время та же новость снова новость."""
    talker = Announcer(min_gap_s=0.0, repeat_after_s=0.01)
    talker.offer("тебе написала мама")
    time.sleep(0.02)
    assert talker.offer("тебе написала мама") == "say"


def test_empty_text_is_never_spoken() -> None:
    """Пустую реплику произносить нечем и незачем."""
    assert Announcer().offer("   ") == "drop"


# --- ничего не теряется -----------------------------------------------------


async def test_held_is_said_at_the_next_conversation() -> None:
    """Придержанное досказывается, когда владелец сам заговорил.

    Момент выбран не случайно: человек, обратившийся к ассистенту, заведомо
    слушает. Будить его ради накопленного было бы ровно тем, от чего вся эта
    политика и защищает.
    """
    events = LocalEventBus()
    heard: list[str] = []

    async def listen(event: AnnouncementRequested) -> None:
        heard.append(event.text)

    events.subscribe(AnnouncementRequested.NAME, listen)  # type: ignore[arg-type]

    modes = Modes()
    modes.on(DEAF, minutes=30)
    announcer = Announcer(events=events, modes=modes, min_gap_s=0.0)

    announcer.offer("готово: разобрать логи", importance=NORMAL)
    assert heard == []

    modes.off(DEAF)
    assert announcer.flush() == 1
    # Шина разносит событие отдельной задачей: одного шага цикла ей мало,
    # надо дать ей действительно отработать.
    await asyncio.sleep(0.05)
    assert heard == ["готово: разобрать логи"]
    assert not announcer.held


def test_flush_on_empty_says_nothing() -> None:
    """Досказывать нечего — значит ничего и не звучит."""
    assert Announcer().flush() == 0


def test_held_queue_has_a_limit() -> None:
    """Больше пяти новостей никто не дослушает, а устаревшие хуже никаких."""
    off = Announcer(enabled=False)
    for number in range(12):
        off.offer(f"новость {number}", importance=NORMAL)

    assert len(off.held) == 5
    # Остаются самые свежие: старые новости тем и плохи, что устарели.
    assert off.held[-1].text == "новость 11"
