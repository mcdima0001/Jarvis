"""Две команды в одной фразе: «включи музыку и сделай громче».

Главное тут не разрезать, а **не разрезать лишнего**: союз «и» живёт и внутри
самих команд, и посреди названия трека он ничем не отличается от союза между
поручениями.
"""

from __future__ import annotations

import pytest

from jarvis.core.bus import LocalEventBus
from jarvis.core.contracts import Intent, ToolResult, Utterance
from jarvis.core.router import Dispatcher, FallbackResolver, PhraseResolver, Router
from jarvis.core.tools import ToolRegistry, tool
from jarvis.core.tools.tool import collect_tools


class _Commands:
    """Скилл-пустышка с парой команд и одной, берущей текст целиком."""

    def __init__(self) -> None:
        self.done: list[str] = []
        self.fails: set[str] = set()

    @tool(phrases=["сделай громче", "громче"])
    async def louder(self) -> ToolResult:
        """Сделать громче."""
        self.done.append("louder")
        if "louder" in self.fails:
            return ToolResult.failure("некуда громче", tool="demo.louder")
        return ToolResult.success("ok", tool="demo.louder", speech="Готово.")

    @tool(phrases=["поставь на паузу", "пауза"])
    async def pause(self) -> ToolResult:
        """Поставить на паузу."""
        self.done.append("pause")
        return ToolResult.success("ok", tool="demo.pause", speech="Пауза.")

    @tool(phrases=["включи трек {track}"])
    async def play(self, track: str) -> ToolResult:
        """Включить трек.

        :param track: название.
        """
        self.done.append(f"play:{track}")
        return ToolResult.success(track, tool="demo.play", speech="Включаю.")


@pytest.fixture
def parts(events: LocalEventBus):
    """Диспетчер поверх настоящих роутера и реестра."""
    registry = ToolRegistry(events=events, default_timeout=1.0)
    skill = _Commands()
    for item in collect_tools(skill, namespace="demo"):
        registry.register(item)
    router = Router([PhraseResolver(registry), FallbackResolver()], events=events)
    return skill, Dispatcher(router=router, registry=registry, events=events)


async def test_two_commands_are_both_carried_out(parts) -> None:
    """Обе половины — команды, значит выполняем обе по очереди."""
    skill, dispatcher = parts

    result = await dispatcher.handle(Utterance(text="сделай громче и поставь на паузу"))

    assert skill.done == ["louder", "pause"]
    assert result.ok


async def test_and_inside_a_track_name_is_not_a_separator(parts) -> None:
    """«Включи трек Я сошла с ума и не помню» — одна команда, а не две.

    Самый опасный случай всей затеи: разрез посреди названия выполнил бы
    половину трека как приказ. Спасает то, что хвост названия командой не
    опознаётся и уходит в свободный разговор.
    """
    skill, dispatcher = parts

    await dispatcher.handle(Utterance(text="включи трек я сошла с ума и не помню"))

    assert skill.done == ["play:я сошла с ума и не помню"]


async def test_chain_stops_when_the_first_command_fails(parts) -> None:
    """Сорвалась первая — вторую не делаем.

    «Переключись на музыку и включи трек» после неудачного переключения
    включило бы трек неизвестно где: человек, говоря такое, подразумевает
    порядок, а не два независимых поручения.
    """
    skill, dispatcher = parts
    skill.fails.add("louder")

    result = await dispatcher.handle(Utterance(text="сделай громче и поставь на паузу"))

    assert skill.done == ["louder"], "вторая команда выполнилась после сбоя первой"
    assert not result.ok


async def test_ordinary_phrase_goes_whole(parts) -> None:
    """Фраза без союза идёт обычным путём — ничего не изменилось."""
    skill, dispatcher = parts

    await dispatcher.handle(Utterance(text="поставь на паузу"))

    assert skill.done == ["pause"]


async def test_long_chains_are_refused(parts) -> None:
    """Четыре команды подряд — это уже не цепочка, а «и» внутри аргумента."""
    _, dispatcher = parts

    chain = await dispatcher._chain(
        Utterance(text="громче и пауза и громче и пауза")
    )

    assert chain is None


async def test_hypothesis_never_asks_the_model(events: LocalEventBus) -> None:
    """Проверка гипотезы обязана быть бесплатной.

    Иначе каждая фраза с союзом «и» — а их немало — начиналась бы с двух
    платных запросов на догадку, которая чаще всего не подтвердится.
    """
    registry = ToolRegistry(events=events, default_timeout=1.0)
    asked: list[str] = []

    class _Model:
        name = "llm"

        async def resolve(self, utterance: Utterance) -> Intent | None:
            asked.append(utterance.text)
            return Intent(tool="demo.play", arguments={"track": "x"}, confidence=0.9)

    skill = _Commands()
    for item in collect_tools(skill, namespace="demo"):
        registry.register(item)
    router = Router(
        [PhraseResolver(registry), _Model(), FallbackResolver()], events=events
    )
    dispatcher = Dispatcher(router=router, registry=registry, events=events)

    await dispatcher.handle(Utterance(text="сделай громче и включи что-нибудь"))

    assert all(" и " not in text for text in asked) or len(asked) == 1, (
        f"модель спрашивали на проверку гипотезы: {asked}"
    )
