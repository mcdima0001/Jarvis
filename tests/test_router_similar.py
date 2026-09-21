"""Ослышка в одном слове: «табах басов» — это «добавь басов» (21.09.2026)."""

from __future__ import annotations

import pytest

from jarvis.core.contracts import ToolResult, Utterance
from jarvis.core.router.resolvers import SimilarResolver
from jarvis.core.tools import ToolRegistry, collect_tools, tool


class Sound:
    @tool(phrases=["добавь басов", "больше басов"], reversible=True)
    async def more_bass(self) -> ToolResult:
        """Добавить басов."""
        return ToolResult.success(None)

    @tool(phrases=["убери басы"], reversible=True)
    async def less_bass(self) -> ToolResult:
        """Убрать басы."""
        return ToolResult.success(None)

    @tool(phrases=["открой телефон"], reversible=True)
    async def phone(self) -> ToolResult:
        """Открыть телефон."""
        return ToolResult.success(None)

    @tool(phrases=["открой телеграм"], reversible=True)
    async def telegram(self) -> ToolResult:
        """Открыть телеграм."""
        return ToolResult.success(None)

    @tool(phrases=["что с эквалайзером"], reversible=True)
    async def status(self) -> ToolResult:
        """Состояние эквалайзера."""
        return ToolResult.success(None)

    @tool(phrases=["проверь скорость интернета"], reversible=True)
    async def speed(self) -> ToolResult:
        """Скорость интернета."""
        return ToolResult.success(None)


@pytest.fixture
def resolver() -> SimilarResolver:
    registry = ToolRegistry()
    for item in collect_tools(Sound(), namespace="sound"):
        registry.register(item)
    return SimilarResolver(registry)


async def _tool(resolver: SimilarResolver, text: str) -> str | None:
    intent = await resolver.resolve(Utterance(text=text, named=True))
    return intent.tool if intent else None


async def test_one_misheard_word_finds_the_command(resolver: SimilarResolver) -> None:
    """Живая жалоба владельца: распознавание слышит «табах» вместо «добавь»."""
    assert await _tool(resolver, "табах басов") == "sound.more_bass"
    assert await _tool(resolver, "Табак Басов") == "sound.more_bass"
    assert await _tool(resolver, "убери босов") == "sound.less_bass"


async def test_another_command_is_not_a_mishearing(resolver: SimilarResolver) -> None:
    """«Телеграм» — слово другой команды, и подменять его телефоном нельзя."""
    assert await _tool(resolver, "открой телеграм") is None
    assert await _tool(resolver, "открой телефон") is None, "точное совпадение — дело phrase"


async def test_service_words_are_not_an_anchor(resolver: SimilarResolver) -> None:
    """Совпали «что» и «с» — это не опора, иначе интернет станет эквалайзером."""
    assert await _tool(resolver, "что с интернетом") is None
    assert await _tool(resolver, "как дела") is None


async def test_a_single_word_is_never_guessed(resolver: SimilarResolver) -> None:
    """Из одного слова ослышка неотличима от другой команды целиком."""
    assert await _tool(resolver, "басов") is None
    assert await _tool(resolver, "убери") is None


async def test_a_different_phrase_is_left_to_the_model(resolver: SimilarResolver) -> None:
    assert await _tool(resolver, "почему небо голубое") is None
    assert await _tool(resolver, "расскажи анекдот про басы") is None
