"""Ответ модели вслух по мере написания: резка, провайдер, сервис, конвейер."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from jarvis.core.bus import LocalEventBus
from jarvis.core.config import ProviderConfig, TaskProfile
from jarvis.core.contracts import LIVE_SPEECH, ToolResult, Utterance
from jarvis.core.contracts.events import AssistantReplied
from jarvis.core.errors import LLMError
from jarvis.core.llm import LLMService, ProfileRegistry
from jarvis.core.llm.protocol import LLMRequest, LLMResponse, Message
from jarvis.core.llm.providers import OpenAIProvider
from jarvis.core.text.sentences import SentenceSplitter
from jarvis.core.tools import ToolRegistry, collect_tools, tool
from tests.test_voice import RecordingTTS, _pipeline

# --- резка на предложения ----------------------------------------------------


def test_sentences_come_out_as_soon_as_they_are_complete() -> None:
    splitter = SentenceSplitter()
    assert splitter.push("Добрый вечер, сэр. Сегод") == []  # короткое ждёт соседа
    assert splitter.push("ня в Твери пятнадцать градусов. Ве") == [
        "Добрый вечер, сэр. Сегодня в Твери пятнадцать градусов."
    ]
    assert splitter.push("тер слабый, дождя не будет, можно гулять!") == []
    assert splitter.flush() == "Ветер слабый, дождя не будет, можно гулять!"


def test_number_with_a_dot_is_not_the_end_of_a_sentence() -> None:
    splitter = SentenceSplitter(min_chars=1)
    assert splitter.push("Курс доллара 91.5 рубля") == []
    assert splitter.push(" на сегодня. Дальше") == ["Курс доллара 91.5 рубля на сегодня."]


# --- провайдер отдаёт поток ------------------------------------------------------


def _sse(*events: dict[str, Any]) -> bytes:
    lines = [f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events]
    return ("".join(lines) + "data: [DONE]\n\n").encode()


def _provider(handler) -> OpenAIProvider:
    provider = OpenAIProvider(ProviderConfig(name="openai", type="openai", api_key="k"))
    provider._client = httpx.AsyncClient(base_url="https://openai.test/v1", transport=httpx.MockTransport(handler))
    return provider


async def test_provider_streams_text_and_reports_usage_last() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=_sse(
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "Добрый "}}]},
            {"choices": [{"delta": {"content": "вечер."}}]},
            {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 3}},
        ))

    usage: dict[str, Any] = {}
    request = LLMRequest(messages=[Message.user("привет")], model="gpt-5.4-mini", max_tokens=50)
    pieces = [piece async for piece in _provider(handler).stream(request, usage)]
    assert pieces == ["Добрый ", "вечер."]
    assert usage == {"prompt_tokens": 40, "completion_tokens": 3}
    assert seen["stream"] is True and seen["stream_options"] == {"include_usage": True}
    assert seen["max_completion_tokens"] == 50, "поправки провайдера доходят и до потока"


async def test_provider_stream_refusal_is_an_error() -> None:
    provider = _provider(lambda request: httpx.Response(500, json={"error": {"message": "упало"}}))
    request = LLMRequest(messages=[Message.user("привет")], model="m")
    with pytest.raises(LLMError):
        [piece async for piece in provider.stream(request, {})]


# --- сервис ----------------------------------------------------------------------


class _Provider:
    name = "fake"
    configured = True

    def __init__(self, pieces: list[str] | None) -> None:
        self.pieces = pieces
        self.completed = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.completed += 1
        return LLMResponse(text="Целиком.", usage={"prompt_tokens": 5, "completion_tokens": 1})

    async def stream(self, request: LLMRequest, usage: dict[str, Any]) -> AsyncIterator[str]:
        if self.pieces is None:
            raise LLMError("поток не открылся")
        for piece in self.pieces:
            yield piece
        usage.update({"prompt_tokens": 10, "completion_tokens": 2})

    async def aclose(self) -> None: ...


def _service(provider: _Provider) -> LLMService:
    profile = TaskProfile(task="dialog", provider="fake", model="stub")
    return LLMService(providers={"fake": provider}, profiles=ProfileRegistry({"dialog": profile}, default_task="dialog"))


async def test_service_streams_and_counts_the_spending() -> None:
    service = _service(_Provider(["Раз. ", "Два."]))
    assert [piece async for piece in service.ask_stream("вопрос", task="dialog")] == ["Раз. ", "Два."]
    assert service.spending.total_tokens == 12


async def test_service_falls_back_to_a_whole_answer_when_the_stream_fails_at_once() -> None:
    provider = _Provider(None)
    service = _service(provider)
    assert [piece async for piece in service.ask_stream("вопрос", task="dialog")] == ["Целиком."]
    assert provider.completed == 1


# --- конвейер произносит по предложению ----------------------------------------


class _Talker:
    """Инструмент, который отвечает потоком — если вызывающий его ждёт."""

    def __init__(self) -> None:
        self.live: bool | None = None

    @tool(phrases=["расскажи сказку"], reversible=True)
    async def tale(self) -> ToolResult:
        """Рассказать сказку."""
        self.live = LIVE_SPEECH.get()

        async def pieces() -> AsyncIterator[str]:
            for piece in ("Жил-был на свете ", "один старый робот. ", "Он очень любил ", "говорить по делу."):
                yield piece

        return ToolResult(ok=True, speech_stream=pieces())


async def test_pipeline_speaks_sentence_by_sentence_and_replies_once() -> None:
    registry, events = ToolRegistry(), LocalEventBus()
    talker = _Talker()
    for item in collect_tools(talker, namespace="talk"):
        registry.register(item)
    tts = RecordingTTS()
    pipeline = _pipeline(registry, events, tts=tts)
    replied: list[AssistantReplied] = []
    emit = pipeline._events.emit

    def spy(event: Any) -> None:
        if isinstance(event, AssistantReplied):
            replied.append(event)
        emit(event)

    pipeline._events.emit = spy  # type: ignore[method-assign]

    await pipeline.handle(Utterance(text="расскажи сказку", named=True))

    assert talker.live is True, "конвейер объявляет, что умеет говорить по ходу"
    assert tts.said == ["Жил-был на свете один старый робот.", "Он очень любил говорить по делу."]
    assert [event.text for event in replied if event.source == "voice"] == [
        "Жил-был на свете один старый робот. Он очень любил говорить по делу."
    ], "«ответил» — один раз и с полным текстом, иначе громкость поднимется между предложениями"
    assert pipeline.last_reply == "Жил-был на свете один старый робот. Он очень любил говорить по делу."
    assert LIVE_SPEECH.get() is False, "признак снимается после команды"


async def test_broken_stream_before_the_first_sentence_says_it_failed() -> None:
    registry, events = ToolRegistry(), LocalEventBus()

    class Broken:
        @tool(phrases=["расскажи сказку"], reversible=True)
        async def tale(self) -> ToolResult:
            """Рассказать сказку."""

            async def pieces() -> AsyncIterator[str]:
                raise LLMError("сеть")
                yield ""

            return ToolResult(ok=True, speech_stream=pieces())

    for item in collect_tools(Broken(), namespace="talk"):
        registry.register(item)
    tts = RecordingTTS()
    pipeline = _pipeline(registry, events, tts=tts)
    await pipeline.handle(Utterance(text="расскажи сказку", named=True))
    assert len(tts.said) == 1 and tts.said[0], "вслух — реплика о неудаче, а не тишина"
