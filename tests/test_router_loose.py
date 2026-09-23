"""Лишние слова внутри команды: «поставь В таймер на 5 минут» (23.09.2026).

Живой случай владельца: та же команда с одним лишним предлогом ушла в свободный
разговор. Замысел — разбирать фразу по опорным словам, допуская между ними
мусор. Опасность у замысла ровно одна и хорошо известная: слова команд живут и
внутри обычной речи, поэтому правило обязано быть узким.
"""

from __future__ import annotations

import pytest

from jarvis.core.contracts import ToolResult, Utterance
from jarvis.core.router.resolvers import LooseResolver
from jarvis.core.tools import ToolRegistry, collect_tools, tool


class Skill:
    @tool(phrases=["поставь таймер на {request}", "поставь таймер"], reversible=True)
    async def timer(self, request: str = "") -> ToolResult:
        """Засечь время."""
        return ToolResult.success(None)

    @tool(phrases=["напиши {who} {text}"], reversible=False)
    async def send(self, who: str = "", text: str = "") -> ToolResult:
        """Отправить сообщение."""
        return ToolResult.success(None)

    @tool(phrases=["включи музыку"], reversible=True)
    async def play(self) -> ToolResult:
        """Включить музыку."""
        return ToolResult.success(None)

    @tool(phrases=["отмени таймер"], reversible=False)
    async def cancel(self) -> ToolResult:
        """Отменить таймер."""
        return ToolResult.success(None)


@pytest.fixture
def resolver() -> LooseResolver:
    registry = ToolRegistry()
    for item in collect_tools(Skill(), namespace="clock"):
        registry.register(item)
    return LooseResolver(registry)


async def test_one_extra_word_no_longer_breaks_the_command(resolver: LooseResolver) -> None:
    """Тот самый случай: разница в двух буквах, а результат был противоположный."""
    intent = await resolver.resolve(Utterance(text="поставь в таймер на 5 минут"))
    assert intent is not None and intent.tool == "clock.timer"
    assert intent.arguments == {"request": "5 минут"}


async def test_a_whole_handful_of_junk_still_works(resolver: LooseResolver) -> None:
    """Пример владельца слово в слово: «поставь ты уже блин этот таймер…»."""
    intent = await resolver.resolve(
        Utterance(text="поставь ты уже блин этот таймер на 5 гребаных минут")
    )
    assert intent is not None and intent.tool == "clock.timer"
    assert intent.arguments == {"request": "5 гребаных минут"}


async def test_the_command_must_start_the_phrase(resolver: LooseResolver) -> None:
    """Иначе диктовка перестаёт быть диктовкой: слова команд живут и в речи."""
    said = "напиши маме поставь таймер на 5 минут"
    intent = await resolver.resolve(Utterance(text=said))
    assert intent is None or intent.tool != "clock.timer"


async def test_negation_inside_forbids_the_match(resolver: LooseResolver) -> None:
    """«Поставь чайник, а таймер не надо» — это не таймер."""
    assert await resolver.resolve(Utterance(text="поставь чайник а таймер не надо")) is None


async def test_short_words_are_compared_letter_by_letter(resolver: LooseResolver) -> None:
    """У «на» и «не» одинаковая основа — и на этом таймер однажды сработал."""
    assert await resolver.resolve(Utterance(text="поставь таймер не 5 минут")) is None


async def test_junk_between_every_word_is_not_a_command(resolver: LooseResolver) -> None:
    """Мусор в каждом промежутке — это другая фраза из тех же слов."""
    said = "поставь как-нибудь потом вечером таймер если не сложно на 5 минут"
    assert await resolver.resolve(Utterance(text=said)) is None


async def test_a_clean_command_is_left_to_the_phrase_resolver(resolver: LooseResolver) -> None:
    """Без мусора этот резолвер молчит: точные фразы разбираются строго."""
    assert await resolver.resolve(Utterance(text="поставь таймер на 5 минут")) is None


async def test_politeness_inside_a_short_command(resolver: LooseResolver) -> None:
    intent = await resolver.resolve(Utterance(text="включи мне пожалуйста музыку"))
    assert intent is not None and intent.tool == "clock.play"
