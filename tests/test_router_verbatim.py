"""Дословный захват: приставка-глагол, а хвост — свободный текст как есть.

Это чинит конкретную живую поломку: «впиши, что работает. Пусть даст доступ к
LLM» разбор усёк до «что работает». Теперь весь хвост уходит в инструмент
дословно, мимо шаблонов и модели.
"""

from __future__ import annotations

import pytest

from jarvis.core.config import VerbatimRule
from jarvis.core.contracts import Utterance
from jarvis.core.router import VerbatimResolver

_RULES = (
    VerbatimRule(words=frozenset({"впиши", "набери"}), tool="keys.type_text", arg="text"),
)


@pytest.mark.asyncio
async def test_whole_tail_is_captured_verbatim() -> None:
    """Весь хвост после приставки уходит в аргумент, с запятыми и точками."""
    resolver = VerbatimResolver(_RULES)
    intent = await resolver.resolve(
        Utterance(text="впиши, что работает. Пусть даст доступ к LLM")
    )
    assert intent is not None
    assert intent.tool == "keys.type_text"
    assert intent.arguments["text"] == "что работает. Пусть даст доступ к LLM"


@pytest.mark.asyncio
async def test_punctuation_after_the_verb_does_not_break_it() -> None:
    """Запятая сразу за глаголом больше не роняет захват в модель."""
    resolver = VerbatimResolver(_RULES)
    intent = await resolver.resolve(Utterance(text="Впиши: привет, как дела"))
    assert intent is not None
    assert intent.arguments["text"] == "привет, как дела"


@pytest.mark.asyncio
async def test_verb_alone_is_not_dictation() -> None:
    """Голое «впиши» без текста — не диктовка, пусть решает остальная цепочка."""
    resolver = VerbatimResolver(_RULES)
    assert await resolver.resolve(Utterance(text="впиши")) is None


@pytest.mark.asyncio
async def test_other_verbs_pass_through() -> None:
    """Не приставка — не наше дело, отдаём дальше по цепочке."""
    resolver = VerbatimResolver(_RULES)
    assert await resolver.resolve(Utterance(text="открой браузер")) is None


@pytest.mark.asyncio
async def test_empty_rules_never_fire() -> None:
    """Без правил резолвер молчит всегда."""
    resolver = VerbatimResolver(())
    assert await resolver.resolve(Utterance(text="впиши привет")) is None


def test_shipped_config_routes_dictation_verbatim() -> None:
    """Рабочий конфиг заводит дословный захват на keys.type_text."""
    from pathlib import Path

    from jarvis.core.config import load_config

    root = Path(__file__).resolve().parent.parent
    config = load_config(root / "config" / "config.yaml")
    rules = config.router.verbatim
    assert any(rule.tool == "keys.type_text" and "впиши" in rule.words for rule in rules)
    assert "verbatim" in config.router.resolvers
