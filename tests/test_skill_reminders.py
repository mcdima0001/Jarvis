"""Напоминания: разбор времени, срабатывание, переживание перезапуска.

Разбор времени — чистые функции, поэтому проверяется он подробно: это тот
случай, когда ошибка обнаруживается не сразу, а ровно тогда, когда напоминание
не сработало, и исправлять уже поздно.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jarvis.core.bus import LocalEventBus
from jarvis.core.config import MemoryConfig
from jarvis.core.memory import build_memory
from jarvis.core.situation import Situation
from jarvis.core.skills import SkillContext, SkillScope
from jarvis.core.state import Modes
from jarvis.core.tools import ToolRegistry, collect_tools
from jarvis.core.tts import NullTTS

_ROOT = Path(__file__).resolve().parent.parent
#: Полдень буднего дня: и утро, и вечер от него в разные стороны, поэтому на
#: нём видно обе догадки о «в восемь».
NOW = datetime(2026, 8, 1, 14, 30)


def _load() -> Any:
    """Загрузить скилл как модуль: он плагин и лежит вне пакета."""
    path = _ROOT / "skills" / "reminders" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_reminders", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


skill_module = _load()


# --- разбор времени ---------------------------------------------------------


@pytest.mark.parametrize(
    "request_text, expected, body",
    [
        ("через 20 минут позвонить маме", NOW + timedelta(minutes=20), "позвонить маме"),
        # Время может стоять и в конце — говорят и так, и так.
        ("позвонить маме через 20 минут", NOW + timedelta(minutes=20), "позвонить маме"),
        ("через двадцать минут перезвонить", NOW + timedelta(minutes=20), "перезвонить"),
        ("через полчаса выключить духовку", NOW + timedelta(minutes=30), "выключить духовку"),
        ("через час", NOW + timedelta(hours=1), ""),
        ("через полтора часа проверить рендер", NOW + timedelta(minutes=90), "проверить рендер"),
        ("через 3 часа снять с зарядки", NOW + timedelta(hours=3), "снять с зарядки"),
        ("в 18:30 позвонить в студию", NOW.replace(hour=18, minute=30), "позвонить в студию"),
        ("in 15 minutes call mom", NOW + timedelta(minutes=15), "call mom"),
    ],
)
def test_time_is_parsed(request_text: str, expected: datetime, body: str) -> None:
    """Обычные формулировки разбираются кодом, без всякой модели."""
    when, text = skill_module.parse_when(request_text, NOW)
    assert when is not None, request_text
    assert abs((when - expected).total_seconds()) < 1, request_text
    assert text == body


def test_named_time_rolls_over_to_tomorrow() -> None:
    """Названное время без даты — ближайшее такое, а не сегодняшнее прошедшее."""
    when, _ = skill_module.parse_when("в 9 утра встать", NOW)
    assert when == NOW.replace(hour=9, minute=0) + timedelta(days=1)


def test_evening_is_the_cheaper_guess() -> None:
    """«В восемь», сказанное днём, — это вечер.

    Одно из двух пониманий выбрать всё равно придётся. Ошибка в вечер даёт
    напоминание раньше нужного (переставят), ошибка в завтрашнее утро — на
    двенадцать часов позже, то есть бесполезное.
    """
    when, _ = skill_module.parse_when("в 8 позвонить", NOW)
    assert when == NOW.replace(hour=20, minute=0)

    # Утро ещё впереди — значит его и имели в виду.
    morning = datetime(2026, 8, 1, 9, 30)
    when, _ = skill_module.parse_when("в 11 позвонить", morning)
    assert when == morning.replace(hour=11, minute=0)

    # А поздним вечером восьми сегодня уже не будет.
    late = datetime(2026, 8, 1, 22, 30)
    when, _ = skill_module.parse_when("в 8 позвонить", late)
    assert when == late.replace(hour=8, minute=0) + timedelta(days=1)


def test_explicit_words_switch_the_guess_off() -> None:
    """«Утра» и «завтра» сказаны прямо — догадываться больше не о чем."""
    assert skill_module.parse_when("в 9 утра позвонить", NOW)[0].hour == 9
    assert skill_module.parse_when("завтра в 9 позвонить", NOW)[0].day == 2
    assert skill_module.parse_when("в 9 вечера кино", NOW)[0].hour == 21
    assert skill_module.parse_when("в 2 ночи бэкап", NOW)[0].hour == 2


def test_unparsed_time_is_refused_not_guessed() -> None:
    """Не разобрали — говорим об этом, а не ставим наугад.

    Наугад тут хуже всего: человек понадеялся и узнает об ошибке ровно тогда,
    когда напоминание не сработает.
    """
    when, _ = skill_module.parse_when("в следующий вторник купить кабель", NOW)
    assert when is None


def test_filler_words_do_not_reach_the_reminder() -> None:
    """«Напомни **мне**, **что** нужно…» — эти слова сказаны человеку."""
    _, body = skill_module.parse_when("мне что нужно купить хлеб через 10 минут", NOW)
    assert body == "нужно купить хлеб"


def test_clock_is_spoken_not_printed() -> None:
    """«15:40» синтез читает как «пятнадцать двоеточие сорок»."""
    assert skill_module.clock_phrase(NOW.replace(hour=15, minute=40)) == "15 часов 40 минут"
    assert skill_module.clock_phrase(NOW.replace(hour=21, minute=0)) == "21 час"
    assert skill_module.clock_phrase(NOW.replace(hour=9, minute=1)) == "9 часов 1 минуту"


def test_near_future_is_told_as_a_delay() -> None:
    """Близкое называется сроком, далёкое — временем: так полезнее."""
    assert "через" in skill_module.when_phrase(NOW + timedelta(minutes=5), NOW)
    assert "завтра" in skill_module.when_phrase(NOW + timedelta(days=1), NOW)
    assert skill_module.when_phrase(NOW + timedelta(hours=3), NOW).startswith("в ")


# --- живой скилл ------------------------------------------------------------


def _skill(tmp_path: Path, events: LocalEventBus, registry: ToolRegistry) -> Any:
    """Поднять скилл с настоящей файловой памятью во временном каталоге."""
    memory = build_memory(
        MemoryConfig(
            dir=tmp_path / "memory",
            documents=("reminders",),
            journals=("today",),
            context_budget_tokens=500,
        )
    )
    skill = skill_module.RemindersSkill()
    modes = Modes()
    context = SkillContext(
        skill="reminders",
        config={"check_seconds": 0.05},
        logger=__import__("logging").getLogger("test.reminders"),
        events=events,
        tools=registry,
        memory=memory,
        llm=None,  # type: ignore[arg-type]
        tts=NullTTS(),
        scope=SkillScope(skill="reminders", events=events, tools=registry),
        root=tmp_path,
        modes=modes,
        situation=Situation(modes=modes),
    )
    return skill, context


async def _ready(tmp_path: Path, events: LocalEventBus, registry: ToolRegistry) -> Any:
    """Скилл после setup, с зарегистрированными инструментами."""
    skill, context = _skill(tmp_path, events, registry)
    await skill.setup(context)
    for item in collect_tools(skill, namespace="reminders"):
        registry.register(item)
    return skill


async def test_reminder_is_announced_when_due(
    tmp_path: Path, events: LocalEventBus, registry: ToolRegistry
) -> None:
    """Сработавшее напоминание уходит событием, а не прямо в синтез.

    Произносить обязан голосовой конвейер: только он глушит микрофон на время
    речи. Иначе ассистент услышит собственное напоминание и, попав в окно
    ответа, выполнит его как команду.
    """
    heard: list[Any] = []

    async def note(event: Any) -> None:
        heard.append(event)

    events.subscribe("assistant.announcement", note)
    skill = await _ready(tmp_path, events, registry)

    result = await registry.invoke("reminders.remind", {"request": "через 1 секунду позвонить"})
    assert result.ok

    await asyncio.sleep(1.2)
    await skill._fire_due()
    await asyncio.sleep(0.05)

    assert len(heard) == 1
    assert "позвонить" in heard[0].text


async def test_reminder_survives_a_restart(
    tmp_path: Path, events: LocalEventBus, registry: ToolRegistry
) -> None:
    """Напоминание, умершее вместе с процессом, — худший вид поломки.

    Поэтому срок хранится в стенных часах и лежит на диске, а не в памяти
    процесса и не в монотонном времени, которое перезапуск обнуляет.
    """
    skill = await _ready(tmp_path, events, registry)
    await registry.invoke("reminders.remind", {"request": "через 2 часа проверить рендер"})

    # Новый экземпляр с тем же каталогом памяти — это и есть перезапуск.
    again, context = _skill(tmp_path, events, ToolRegistry(events=events))
    await again.setup(context)

    assert len(again._items) == 1
    assert again._items[0]["text"] == "проверить рендер"


async def test_hopelessly_late_reminder_is_dropped_but_logged(
    tmp_path: Path,
    events: LocalEventBus,
    registry: ToolRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Просроченное на полсуток не произносим, но и молча не теряем.

    Напоминание, просроченное на пятнадцать часов, среди ночи скорее пугает,
    чем помогает. А молча терять обещанное нельзя — иначе разбирать нечего.
    """
    heard: list[Any] = []

    async def note(event: Any) -> None:
        heard.append(event)

    events.subscribe("assistant.announcement", note)
    skill = await _ready(tmp_path, events, registry)
    skill._items = [
        {
            "id": 1,
            "at": (datetime.now() - timedelta(hours=20)).timestamp(),
            "text": "давно прошедшее",
            "kind": "reminder",
            "language": "ru",
            "minutes": 0,
        }
    ]

    with caplog.at_level("WARNING"):
        await skill._fire_due()
    await asyncio.sleep(0.05)

    assert heard == []
    assert "давно прошедшее" in caplog.text
    assert skill._items == []


async def test_slightly_late_reminder_names_its_time(
    tmp_path: Path, events: LocalEventBus, registry: ToolRegistry
) -> None:
    """Опоздавшее произносится вместе со временем: по нему и понятно, о чём речь."""
    heard: list[Any] = []

    async def note(event: Any) -> None:
        heard.append(event)

    events.subscribe("assistant.announcement", note)
    skill = await _ready(tmp_path, events, registry)
    was = datetime.now() - timedelta(minutes=40)
    skill._items = [
        {"id": 1, "at": was.timestamp(), "text": "забрать заказ",
         "kind": "reminder", "language": "ru", "minutes": 0}
    ]

    await skill._fire_due()
    await asyncio.sleep(0.05)

    assert len(heard) == 1
    assert "забрать заказ" in heard[0].text
    assert skill_module.clock_phrase(was) in heard[0].text


async def test_timer_reports_its_length(
    tmp_path: Path, events: LocalEventBus, registry: ToolRegistry
) -> None:
    """Таймер называет длительность, а не время суток: спрашивали про неё."""
    skill = await _ready(tmp_path, events, registry)
    result = await registry.invoke("reminders.timer", {"minutes": "10"})

    assert result.ok
    assert result.speech_for("ru") == "Таймер на 10 минут."
    assert skill._items[0]["kind"] == "timer"


async def test_pending_and_cancel(
    tmp_path: Path, events: LocalEventBus, registry: ToolRegistry
) -> None:
    """Список и отмена: и то и другое обязано быть, раз напоминания невидимы."""
    skill = await _ready(tmp_path, events, registry)
    await registry.invoke("reminders.remind", {"request": "через 2 часа купить хлеб"})
    await registry.invoke("reminders.remind", {"request": "через 3 часа позвонить маме"})

    listed = await registry.invoke("reminders.pending")
    assert len(listed.value) == 2
    assert "хлеб" in listed.speech_for("ru")

    one = await registry.invoke("reminders.cancel", {"which": "хлеб"})
    assert one.ok
    assert len(skill._items) == 1

    missing = await registry.invoke("reminders.cancel", {"which": "кабель"})
    assert not missing.ok, "нечего отменять — говорим об этом, а не молчим"

    everything = await registry.invoke("reminders.cancel")
    assert everything.value == {"cancelled": 1}
    assert skill._items == []


async def test_reminder_without_time_asks_instead_of_guessing(
    tmp_path: Path, events: LocalEventBus, registry: ToolRegistry
) -> None:
    """Без времени — переспрашиваем; поставить наугад значит подвести."""
    await _ready(tmp_path, events, registry)
    result = await registry.invoke("reminders.remind", {"request": "купить кабель XLR"})

    assert not result.ok
    assert "когда" in result.speech_for("ru").lower()


async def test_missing_memory_section_does_not_break_the_skill(
    tmp_path: Path, events: LocalEventBus, registry: ToolRegistry
) -> None:
    """Раздел памяти не объявлен — работаем без сохранения, а не падаем.

    Терять напоминания при перезапуске плохо; не иметь их вовсе — хуже.
    """
    skill, context = _skill(tmp_path, events, registry)
    memory = build_memory(
        MemoryConfig(
            dir=tmp_path / "other",
            documents=("profile",),
            journals=("today",),
            context_budget_tokens=500,
        )
    )
    await skill.setup(
        SkillContext(
            skill=context.skill,
            config=context.config,
            logger=context.logger,
            events=context.events,
            tools=context.tools,
            memory=memory,
            llm=context.llm,
            tts=context.tts,
            scope=context.scope,
            root=context.root,
            modes=context.modes,
            situation=context.situation,
        )
    )
    for item in collect_tools(skill, namespace="reminders"):
        registry.register(item)

    result = await registry.invoke("reminders.remind", {"request": "через час проверить"})

    assert result.ok
    assert skill._persist is False
    assert (await skill.health()).detail.endswith("перезапуск их не переживёт")
