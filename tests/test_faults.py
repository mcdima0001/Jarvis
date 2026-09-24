"""Сбой называется вслух: пустой счёт — не «не справился, сэр»."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from jarvis.core.bus import LocalEventBus
from jarvis.core.config import ProviderConfig, TaskProfile
from jarvis.core.contracts import ToolResult, Utterance
from jarvis.core.errors import LLMError, LLMNotConfigured, LLMOutOfCredits
from jarvis.core.faults import NO_MONEY, NO_NETWORK, Faults
from jarvis.core.llm import LLMService, ProfileRegistry
from jarvis.core.llm.protocol import Message
from jarvis.core.llm.providers import OpenAIProvider
from jarvis.core.persona import Persona
from jarvis.core.tools import ToolRegistry, collect_tools, tool
from tests.test_voice import RecordingTTS, _pipeline

#: Так OpenAI ответил владельцу 21.09.2026: 429, а код — не тот, что раньше.
NO_CREDITS = {
    "error": {
        "message": "You have no credits remaining.",
        "type": "insufficient_quota",
        "param": None,
        "code": "credit_balance_exhausted",
    }
}


def _service(handler) -> LLMService:
    provider = OpenAIProvider(ProviderConfig(name="openai", type="openai", api_key="k"))
    provider._client = httpx.AsyncClient(
        base_url="https://openai.test/v1", transport=httpx.MockTransport(handler)
    )
    return LLMService(
        providers={"openai": provider},
        profiles=ProfileRegistry(
            {"dialog": TaskProfile(task="dialog", provider="openai", model="gpt-test")},
            default_task="dialog",
        ),
    )


async def test_empty_account_is_recognised_by_type_not_only_by_code() -> None:
    """Проверка по одному `code` два дня выдавала пустой счёт за обычный сбой."""
    service = _service(lambda request: httpx.Response(429, json=NO_CREDITS))
    with pytest.raises(LLMOutOfCredits) as caught:
        await service.complete([Message.user("привет")], task="dialog")
    assert caught.value.provider == "OpenAI"
    fault = service.faults.recent()
    assert fault is not None and fault.kind == NO_MONEY
    assert fault.tellable, "про пустой счёт сказать есть что"
    assert fault.provider == "OpenAI", "чей счёт — в лог и в панель, но не вслух"


async def test_a_good_answer_forgets_the_fault() -> None:
    answers = [
        httpx.Response(429, json=NO_CREDITS),
        httpx.Response(200, json={"choices": [{"message": {"content": "готово"}}], "usage": {}}),
    ]
    service = _service(lambda request: answers.pop(0))
    with pytest.raises(LLMOutOfCredits):
        await service.complete([Message.user("привет")], task="dialog")
    # Пустой счёт запоминается на полминуты: ходить к нему на каждой фразе
    # незачем, он ответит тем же отказом. В жизни срок снимает время.
    assert service._blocked, "к мёртвому провайдеру сразу не возвращаемся"
    service._blocked.clear()
    await service.complete([Message.user("привет")], task="dialog")
    assert service.faults.recent() is None, "счёт пополнили — старую жалобу забыть"


def test_unknown_failures_stay_silent() -> None:
    faults = Faults()
    faults.note(LLMError("что-то пошло не так"))
    assert faults.recent() is None, "о непонятном лучше молчать, чем пугать техникой"
    assert faults.note(LLMNotConfigured("нет ключа")).tellable
    assert faults.note(OSError("Сеть недоступна при обращении к OpenAI")).kind == NO_NETWORK


async def test_the_pipeline_says_why_instead_of_a_polite_refusal() -> None:
    registry, events = ToolRegistry(), LocalEventBus()
    faults = Faults()

    class Broken:
        @tool(phrases=["расскажи сказку"], reversible=True)
        async def tale(self) -> ToolResult:
            """Рассказать сказку."""

            async def pieces() -> AsyncIterator[str]:
                raise LLMOutOfCredits("кончились деньги", provider="OpenAI")
                yield ""

            return ToolResult(ok=True, speech_stream=pieces())

        @tool(phrases=["скажи погоду"], reversible=True)
        async def weather(self) -> ToolResult:
            """Ответить сразу неудачей."""
            faults.note(LLMOutOfCredits("кончились деньги", provider="OpenAI"))
            return ToolResult.failure("LLMOutOfCredits: подробности для лога")

    for item in collect_tools(Broken(), namespace="talk"):
        registry.register(item)
    tts = RecordingTTS()
    pipeline = _pipeline(registry, events, tts=tts, faults=faults)

    # Слова выбирает персона по виду сбоя. Провайдера вслух не называем
    # (просьба владельца 23.09.2026): его имя человеку ничего не говорит, а
    # чинить он идёт в панель и в лог, где причина названа точно.
    excuses = set(Persona().lines(NO_MONEY, "ru"))

    await pipeline.handle(Utterance(text="расскажи сказку", named=True))
    assert tts.said[-1] in excuses, tts.said[-1]

    await pipeline.handle(Utterance(text="скажи погоду", named=True))
    assert tts.said[-1] in excuses, "причина важнее текста ошибки"
    assert "OpenAI" not in tts.said[-1], "имя провайдера вслух не звучит"


async def test_the_same_trouble_does_not_sound_the_same_twice() -> None:
    """Просьба владельца 23.09.2026: «не одной и той же фразой»."""
    registry, events = ToolRegistry(), LocalEventBus()
    faults = Faults()

    class Broken:
        @tool(phrases=["скажи погоду"], reversible=True)
        async def weather(self) -> ToolResult:
            """Ответить сразу неудачей."""
            faults.note(LLMOutOfCredits("кончились деньги", provider="OpenAI"))
            return ToolResult.failure("LLMOutOfCredits: подробности для лога")

    for item in collect_tools(Broken(), namespace="talk"):
        registry.register(item)
    tts = RecordingTTS()
    pipeline = _pipeline(registry, events, tts=tts, faults=faults)
    for _ in range(4):
        await pipeline.handle(Utterance(text="скажи погоду", named=True))
    assert len(set(tts.said)) > 1, "четыре неудачи подряд — четыре одинаковых фразы"
