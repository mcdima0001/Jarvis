"""Бытовой разговор без сети: «как дела» не должно уезжать в облако.

Замысел владельца 21.09.2026 — «чтобы даже когда нет интернета, было тяжело
определить, ИИ это или он». Замер по логам за две недели
(`tools/offline_bench.py`): «как дела» сказано тринадцать раз, и каждый раз
фраза уходила в модель. Без денег на счету она пропадала вместе с ней.
"""

from __future__ import annotations

import pytest

from jarvis.core.builtin import CoreTools
from jarvis.core.contracts import Utterance
from jarvis.core.persona import CHATTER, HOW_ARE_YOU, Persona
from jarvis.core.router import PhraseResolver, Router
from jarvis.core.tools import ToolRegistry, collect_tools


@pytest.fixture
def core() -> tuple[ToolRegistry, CoreTools]:
    """Реестр с одними только встроенными инструментами."""
    registry = ToolRegistry()
    tools = CoreTools(
        llm=None,  # type: ignore[arg-type]
        memory=None,  # type: ignore[arg-type]
        registry=registry,
        skills=None,  # type: ignore[arg-type]
        persona=Persona(address={"*": "сэр"}),
    )
    for item in collect_tools(tools, namespace="core"):
        registry.register(item)
    return registry, tools


async def test_how_are_you_never_reaches_the_model(core) -> None:
    """Самая частая пропадавшая фраза разбирается шаблоном — то есть без сети."""
    registry, _ = core
    router = Router([PhraseResolver(registry)])
    intent = await router.route(Utterance(text="как дела"))
    assert intent is not None and intent.tool == "core.how_are_you"


@pytest.mark.parametrize(
    "said, tool",
    [
        ("привет", "core.hello"),
        ("добрый вечер", "core.hello"),
        ("спасибо", "core.thanks"),
        ("молодец", "core.praise"),
        ("ты тут", "core.here"),
        ("пока", "core.bye"),
        ("how are you", "core.how_are_you"),
        ("thank you", "core.thanks"),
    ],
)
async def test_everyday_phrases_are_answered_locally(core, said: str, tool: str) -> None:
    registry, _ = core
    router = Router([PhraseResolver(registry)])
    intent = await router.route(Utterance(text=said))
    assert intent is not None and intent.tool == tool


async def test_goodbye_does_not_shut_the_assistant_down(core) -> None:
    """«Пока» — это вежливость, а не «выключись»: цена ошибки тут велика."""
    registry, _ = core
    router = Router([PhraseResolver(registry)])
    intent = await router.route(Utterance(text="пока"))
    assert intent is not None and intent.tool != "core.shutdown"


async def test_the_answer_comes_with_the_address_and_in_both_languages(core) -> None:
    registry, _ = core
    result = await registry.invoke("core.how_are_you")
    assert result.ok
    russian = result.speech_options("ru")
    english = result.speech_options("en")
    assert len(russian) > 3 and len(english) > 3, "один ответ на слух — сигнал будильника"
    assert all("сэр" in line for line in russian), "обращение подставляет персона"
    assert all("{" not in line for line in russian + english), "подстановки раскрыты"


async def test_the_owner_can_take_the_address_away() -> None:
    """Пустое обращение убирает и запятую перед ним — общее правило персоны."""
    persona = Persona(address={"*": ""})
    assert all(
        line and not line.startswith(",") and "{" not in line
        for situation in CHATTER
        for line in persona.lines(situation)
    )


async def test_small_talk_stays_out_of_the_model_catalog(core) -> None:
    """Каталог оплачивается на каждой неузнанной фразе — вежливости там не место."""
    registry, _ = core
    schemas = str(registry.catalog().function_schemas())
    assert not any(name in schemas for name in ("core.hello", "core.thanks", "core.bye"))


async def test_persona_does_not_repeat_the_same_line(core) -> None:
    """Повторение подряд — главный признак машины, ради этого набор и нужен."""
    persona = Persona(address={"*": "сэр"})
    said = [persona.choose("core.how_are_you", persona.lines(HOW_ARE_YOU)) for _ in range(4)]
    assert len(set(said)) > 1
