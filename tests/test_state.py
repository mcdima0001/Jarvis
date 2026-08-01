"""Режимы и обстановка: состояние, которое живёт между командами.

Проверяется не «включается ли флаг» — это видно и глазами, — а те свойства,
на которых всё держится: срок истекает без всякого тика; из режима «не слушаю»
есть выход; список фраз пробуждения у гейта и у инструмента **один и тот же**;
факты о происходящем не копятся без конца и портятся вовремя.
"""

from __future__ import annotations

import time

import pytest

from jarvis.core.situation import MAX_NOTE, MAX_NOTES, Situation, now_line
from jarvis.core.state import BRIEF, DEAF, WAKE_PHRASES, Modes, wakes_up


# --- режимы ----------------------------------------------------------------


def test_mode_turns_on_and_off() -> None:
    """Самое простое: включили, увидели, выключили."""
    modes = Modes()

    assert not modes.active(DEAF)
    modes.on(DEAF)
    assert modes.active(DEAF)
    assert modes.off(DEAF) is True
    assert not modes.active(DEAF)
    # Выключить то, чего не было, — не ошибка, но и не событие.
    assert modes.off(DEAF) is False


def test_mode_expires_without_any_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Срок истекает сам, при чтении — ни таймера, ни фоновой задачи.

    Ровно на этом свойстве и держится решение не заводить планировщик: режим
    некому поджигать, потому что его и так читают на каждой реплике.
    """
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    modes = Modes()
    modes.on(DEAF, minutes=30)
    assert modes.active(DEAF)

    clock[0] += 29 * 60
    assert modes.active(DEAF), "за минуту до срока режим ещё работает"

    clock[0] += 2 * 60
    assert not modes.active(DEAF), "срок вышел — режим должен исчезнуть сам"
    assert modes.all() == ()


def test_mode_without_deadline_lives_until_told(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нулевой срок — это «пока не скажут иначе», а не «мгновенно»."""
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    modes = Modes()
    modes.on(BRIEF)
    clock[0] += 10 * 3600
    assert modes.active(BRIEF)


def test_clear_reports_what_was_on() -> None:
    """Общий выключатель говорит, что именно выключил.

    Нужно инструменту: «я и так в обычном режиме» и «снова слушаю» — разные
    ответы, и различить их можно только по этому списку.
    """
    modes = Modes()
    assert modes.clear() == ()

    modes.on(DEAF, minutes=5)
    modes.on(BRIEF)
    was = modes.clear()

    assert sorted(mode.name for mode in was) == [BRIEF, DEAF]
    assert modes.all() == ()


def test_describe_names_modes_and_time_left(monkeypatch: pytest.MonkeyPatch) -> None:
    """Описание — для человека и для модели, поэтому по-русски и со сроком."""
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    modes = Modes()

    assert modes.describe("ru") == "", "пустое состояние не занимает места в подсказке"

    modes.on(DEAF, minutes=30)
    text = modes.describe("ru")
    assert "не слушаю" in text
    assert "30" in text, f"срок должен быть назван: {text!r}"
    assert "deaf" not in text, "внутреннее имя человеку ничего не говорит"

    assert "not accepting" in modes.describe("en")


# --- выход из режима «не слушаю» -------------------------------------------


def test_wake_phrases_are_recognized() -> None:
    """Фразы возврата узнаются независимо от регистра и точки в конце."""
    assert wakes_up("проснись")
    assert wakes_up("  Проснись!  ")
    assert wakes_up("как обычно")
    assert wakes_up("wake up")


def test_ordinary_speech_does_not_wake() -> None:
    """Гейт строгий намеренно: случайное слово будить не должно."""
    assert not wakes_up("включи свет")
    assert not wakes_up("проснись пожалуйста завтра пораньше")
    assert not wakes_up("")


def test_gate_and_tool_share_one_list() -> None:
    """Список фраз у гейта и у инструмента обязан быть один и тот же.

    Разъедься они — получится ловушка, которую по логу не увидишь: конвейер
    пропускает фразу, роутер её не узнаёт, реплика уезжает в свободный
    разговор, и разбудить ассистента становится нечем до истечения срока.
    """
    from jarvis.core.builtin import CoreTools
    from jarvis.core.tools import collect_tools

    core = CoreTools(
        llm=None,  # type: ignore[arg-type]
        memory=None,  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]
        skills=None,  # type: ignore[arg-type]
    )
    spec = next(
        item.spec
        for item in collect_tools(core, namespace="core")
        if item.name == "core.as_usual"
    )

    assert spec.phrases == WAKE_PHRASES
    for phrase in WAKE_PHRASES:
        assert wakes_up(phrase), f"{phrase!r} инструмент знает, а гейт нет"


def test_as_usual_stays_out_of_the_catalog() -> None:
    """Инструмент возврата модели не показывается — и это не забывчивость.

    В режиме «не слушаю» до модели вообще ничего не доходит, а каталог уезжает
    в неё на каждой неузнанной фразе, то есть каждый лишний инструмент стоит
    денег постоянно.
    """
    from jarvis.core.builtin import CoreTools
    from jarvis.core.tools import collect_tools

    core = CoreTools(
        llm=None,  # type: ignore[arg-type]
        memory=None,  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]
        skills=None,  # type: ignore[arg-type]
    )
    routable = {
        item.name: item.spec.routable for item in collect_tools(core, namespace="core")
    }

    assert routable["core.as_usual"] is False
    assert routable["core.modes"] is False
    # А вот «полчаса не слушай» шаблоном не выразить — тут модель нужна.
    assert routable["core.sleep"] is True


# --- обстановка -------------------------------------------------------------


def test_situation_always_knows_the_date() -> None:
    """Дата есть всегда: часов у модели нет, а спрашивают про день постоянно."""
    from datetime import datetime

    text = Situation().describe("ru")
    assert str(datetime.now().year) in text
    assert now_line("en").startswith("Current date")


def test_situation_carries_modes() -> None:
    """Режим меняет поведение, поэтому модель обязана про него знать."""
    modes = Modes()
    situation = Situation(modes=modes)

    assert "Режимы" not in situation.describe("ru")

    modes.on(BRIEF)
    assert "Режимы" in situation.describe("ru")
    assert "коротко" in situation.describe("ru")


def test_notes_appear_and_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Факт живёт свой срок и исчезает сам.

    Срок тут не украшение: «открыта Яндекс Музыка» верно десять минут и вредно
    через три часа — модель поверит и уведёт команду на закрытую вкладку.
    """
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    situation = Situation()

    situation.note("открыт сайт", "music.yandex.ru", minutes=20)
    assert "music.yandex.ru" in situation.describe("ru")

    clock[0] += 21 * 60
    assert "music.yandex.ru" not in situation.describe("ru")
    assert situation.notes() == ()


def test_notes_are_bounded_by_construction() -> None:
    """Объём ограничен построением, а не обрезкой готовой строки."""
    situation = Situation()
    for number in range(MAX_NOTES + 4):
        situation.note(f"факт {number}", f"значение {number}")

    notes = situation.notes()
    assert len(notes) == MAX_NOTES
    # Вытесняется самое старое, а не самое новое.
    assert notes[-1].value == f"значение {MAX_NOTES + 3}"


def test_long_note_is_cut_not_dropped() -> None:
    """Слишком длинный факт укорачивается, а не выбрасывается целиком."""
    situation = Situation()
    situation.note("играет", "х" * 500)
    assert len(situation.notes()[0].value) == MAX_NOTE


def test_empty_note_removes_the_fact() -> None:
    """Пустое значение — это «я больше не знаю», а не «знаю пустоту»."""
    situation = Situation()
    situation.note("играет", "Dua Lipa — Levitating")
    situation.note("играет", "")
    assert situation.notes() == ()


def test_repeated_note_moves_to_the_end() -> None:
    """Обновлённый факт становится свежим, иначе его вытеснит первым."""
    situation = Situation()
    situation.note("первый", "1")
    situation.note("второй", "2")
    situation.note("первый", "1 обновлённый")

    assert [note.key for note in situation.notes()] == ["второй", "первый"]


def test_situation_remembers_the_last_command() -> None:
    """«Повтори» и «отмени» без предыдущей команды смысла не имеют."""
    situation = Situation()
    situation.command("включи трек", tool="page.play_item", ok=True)

    text = situation.describe("ru")
    assert "включи трек" in text
    assert "page.play_item" in text
    assert "выполнена" in text

    situation.command("открой канал", tool="page.open_channel", ok=False)
    assert "не удалась" in situation.describe("ru")


# --- инструменты ------------------------------------------------------------


def _memory(tmp_path):
    """Память с теми разделами, которые читает свободный разговор."""
    from jarvis.core.config import MemoryConfig
    from jarvis.core.memory import build_memory

    return build_memory(
        MemoryConfig(
            dir=tmp_path / "memory",
            documents=("profile", "preferences", "studio"),
            journals=("today",),
            context_budget_tokens=500,
        )
    )


def _core(**extra):
    """Собрать встроенные инструменты с заглушками вместо сервисов."""
    from jarvis.core.builtin import CoreTools

    fields = dict(
        llm=None, memory=None, registry=None, skills=None
    )
    fields.update(extra)
    return CoreTools(**fields)  # type: ignore[arg-type]


async def test_sleep_names_the_way_back(registry) -> None:
    """Ответ обязан назвать, чем ассистента включить обратно.

    Режим выключает его целиком, и человек, не услышавший фразы возврата,
    справедливо решит, что ассистент сломался.
    """
    from jarvis.core.tools import collect_tools

    modes = Modes()
    for item in collect_tools(_core(modes=modes), namespace="core"):
        registry.register(item)

    result = await registry.invoke("core.sleep", {"minutes": 30})

    assert result.ok
    assert modes.active(DEAF)
    assert "проснись" in result.speech_for("ru").lower()
    assert "30" in result.speech_for("ru")


async def test_sleep_takes_the_number_from_speech(registry) -> None:
    """«Не слушай 45 минут» доходит числом, а не строкой.

    Шаблон фразы отдаёт аргумент текстом, и без общего приведения типов
    команда отбивалась бы на границе инструмента.
    """
    from jarvis.core.tools import collect_tools

    modes = Modes()
    for item in collect_tools(_core(modes=modes), namespace="core"):
        registry.register(item)

    result = await registry.invoke("core.sleep", {"minutes": "45 минут"})

    assert result.ok
    assert result.value["minutes"] == 45


async def test_as_usual_is_honest_about_doing_nothing(registry) -> None:
    """«Как обычно» в обычном состоянии — не «сделал», а «нечего делать»."""
    from jarvis.core.tools import collect_tools

    modes = Modes()
    for item in collect_tools(_core(modes=modes), namespace="core"):
        registry.register(item)

    quiet = await registry.invoke("core.as_usual")
    assert quiet.ok
    assert quiet.value == []

    modes.on(DEAF, minutes=30)
    modes.on(BRIEF)
    woke = await registry.invoke("core.as_usual")

    assert sorted(woke.value) == [BRIEF, DEAF]
    assert modes.all() == ()


async def test_modes_tool_makes_state_visible(registry) -> None:
    """Режим меняет поведение молча — значит, обязан быть виден по запросу."""
    from jarvis.core.tools import collect_tools

    modes = Modes()
    for item in collect_tools(_core(modes=modes), namespace="core"):
        registry.register(item)

    empty = await registry.invoke("core.modes")
    assert empty.value == []

    modes.on(DEAF, minutes=10)
    listed = await registry.invoke("core.modes")

    assert listed.value[0]["name"] == DEAF
    assert 0 < listed.value[0]["seconds"] <= 600
    assert "не слушаю" in listed.speech_for("ru")


async def test_brief_mode_reaches_the_dialog_prompt(registry, tmp_path) -> None:
    """Режим краткости меняет подсказку разговора, а не только настроение."""
    from jarvis.core.tools import collect_tools

    asked: dict[str, str] = {}

    class Talker:
        """Заглушка модели, которая запоминает системную подсказку."""

        available = True

        async def ask(self, prompt, *, task=None, system=None, context=None):
            asked["system"] = system or ""
            return "Готово."

    modes = Modes()
    core = _core(modes=modes, llm=Talker(), memory=_memory(tmp_path), registry=registry)
    for item in collect_tools(core, namespace="core"):
        registry.register(item)

    await registry.invoke("core.chat", {"text": "как дела"})
    assert "одно предложение" not in asked["system"]

    modes.on(BRIEF)
    await registry.invoke("core.chat", {"text": "как дела"})
    assert "одно предложение" in asked["system"]


async def test_dialog_prompt_carries_the_situation(registry, tmp_path) -> None:
    """Разговор тоже видит обстановку, а не одну только дату."""
    from jarvis.core.tools import collect_tools

    asked: dict[str, str] = {}

    class Talker:
        available = True

        async def ask(self, prompt, *, task=None, system=None, context=None):
            asked["system"] = system or ""
            return "Готово."

    modes = Modes()
    situation = Situation(modes=modes)
    situation.note("играет", "Dua Lipa — Levitating")
    core = _core(
        modes=modes, situation=situation, llm=Talker(), memory=_memory(tmp_path), registry=registry
    )
    for item in collect_tools(core, namespace="core"):
        registry.register(item)

    await registry.invoke("core.chat", {"text": "что это за песня"})

    assert "Levitating" in asked["system"]


# --- обстановка доходит до модели -------------------------------------------


async def test_resolver_shows_the_situation_to_the_model(registry) -> None:
    """Резолвер обязан донести обстановку до разбора, иначе всё зря."""
    from jarvis.core.contracts import Utterance
    from jarvis.core.router import LLMResolver
    from jarvis.core.contracts import ToolResult
    from jarvis.core.tools import collect_tools, tool

    class Lights:
        @tool(phrases=["включи свет"])
        async def on(self) -> ToolResult:
            """Включить свет."""
            return ToolResult.success(True)

    for item in collect_tools(Lights(), namespace="lights"):
        registry.register(item)

    seen: dict[str, str] = {}

    class Model:
        available = True

        async def extract_intent(self, text, catalog, *, task="intent", avoid=(), situation=""):
            seen["situation"] = situation
            return None

    situation = Situation()
    situation.note("открыт сайт", "music.yandex.ru")
    resolver = LLMResolver(registry, Model(), situation=situation)  # type: ignore[arg-type]

    await resolver.resolve(Utterance(text="сделай потише"))

    assert "music.yandex.ru" in seen["situation"]


async def test_dispatcher_records_what_was_asked(registry) -> None:
    """Прошлую команду записывает диспетчер: он один знает и её, и результат."""
    from jarvis.core.contracts import Utterance
    from jarvis.core.router import Dispatcher, PhraseResolver, Router
    from jarvis.core.contracts import ToolResult
    from jarvis.core.tools import collect_tools, tool

    class Lights:
        @tool(phrases=["включи свет"])
        async def on(self) -> ToolResult:
            """Включить свет."""
            return ToolResult.success(True)

    for item in collect_tools(Lights(), namespace="lights"):
        registry.register(item)

    situation = Situation()
    dispatcher = Dispatcher(
        router=Router([PhraseResolver(registry)], threshold=0.6),
        registry=registry,
        situation=situation,
    )

    await dispatcher.handle(Utterance(text="включи свет"))
    assert situation.last is not None
    assert situation.last.tool == "lights.on"
    assert situation.last.ok

    # Неразобранная реплика тоже факт: «повтори» после неё повторять нечего.
    await dispatcher.handle(Utterance(text="сделай мне красиво"))
    assert situation.last.tool == ""
    assert situation.last.ok is False


async def test_common_durations_never_reach_the_model(registry) -> None:
    """«Не слушай 21 минуту» обязано разбираться фразами, а не моделью.

    Окончание у «минуты» своё на каждое число, шаблон же сравнивается целиком —
    и без всех трёх написаний каждая такая просьба уезжала бы в платный разбор.
    Проверка ровно об этом: до модели цепочка не доходит.
    """
    from jarvis.core.contracts import Utterance
    from jarvis.core.router import PhraseResolver
    from jarvis.core.tools import collect_tools

    modes = Modes()
    for item in collect_tools(_core(modes=modes), namespace="core"):
        registry.register(item)
    resolver = PhraseResolver(registry)

    for phrase, minutes in (
        ("не слушай 21 минуту", 21),
        ("не слушай 45 минут", 45),
        ("поспи 2 минуты", 2),
        ("не слушай полчаса", None),
    ):
        intent = await resolver.resolve(Utterance(text=phrase))
        assert intent is not None, f"{phrase!r} ушло бы в модель"
        assert intent.tool == "core.sleep"
        if minutes is not None:
            result = await registry.invoke(intent.tool, intent.arguments)
            assert result.value["minutes"] == minutes, phrase
