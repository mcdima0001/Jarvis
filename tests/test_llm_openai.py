"""Провайдер OpenAI: несовместимые параметры и отказы из-за денег.

Сети тут нет — HTTP подделан. Тексты отказов взяты дословно из замера
13.09.2026 на живом ключе: провайдер разбирает именно их, и выдуманный отказ
проверял бы выдумку.
"""

from __future__ import annotations

import json

import httpx
import pytest

from jarvis.core.config import ProviderConfig
from jarvis.core.errors import LLMError, LLMOutOfCredits
from jarvis.core.llm.protocol import LLMRequest, Message
from jarvis.core.llm.providers import OpenAIProvider, build_provider
from jarvis.core.llm.providers.openai import lesson

#: Ответ, в котором всё хорошо.
FINE = {
    "model": "gpt-5.4-nano",
    "choices": [{"message": {"content": "готово"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 2},
}

#: Отказы OpenAI дословно.
NO_TEMPERATURE = {"error": {
    "message": "Unsupported value: 'temperature' does not support 0.0 with this model. "
               "Only the default (1) value is supported.",
    "type": "invalid_request_error", "param": "temperature", "code": "unsupported_value",
}}
NO_NONE = {"error": {
    "message": "Unsupported value: 'reasoning_effort' does not support 'none' with this "
               "model. Supported values are: 'minimal'.",
    "type": "invalid_request_error", "param": "reasoning_effort", "code": "unsupported_value",
}}
NO_REASONING = {"error": {
    "message": "Unrecognized request argument supplied: reasoning_effort",
    "type": "invalid_request_error", "param": None, "code": None,
}}
BROKE = {"error": {
    "message": "You exceeded your current quota, please check your plan and billing details.",
    "type": "insufficient_quota", "param": None, "code": "insufficient_quota",
}}
TOO_FAST = {"error": {
    "message": "Rate limit reached for gpt-5.4-nano.",
    "type": "requests", "param": None, "code": "rate_limit_exceeded",
}}


def _provider(handler) -> OpenAIProvider:
    config = ProviderConfig(name="openai", type="openai", api_key="test-key")
    provider = OpenAIProvider(config)
    provider._client = httpx.AsyncClient(
        base_url="https://openai.test/v1", transport=httpx.MockTransport(handler)
    )
    return provider


def _asked(**overrides) -> LLMRequest:
    fields = {
        "messages": (Message.user("сделай громче"),),
        "model": "gpt-5.4-nano",
        "temperature": 0.0,
        "max_tokens": 200,
    }
    fields.update(overrides)
    return LLMRequest(**fields)


class _Recorder:
    """Подставной сервер: отвечает по очереди и запоминает, что ему слали."""

    def __init__(self, *answers: tuple[int, dict]) -> None:
        self.answers = list(answers)
        self.sent: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(json.loads(request.content))
        status, body = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return httpx.Response(status, json=body)


# --- что уходит на провод ----------------------------------------------------


async def test_ceiling_is_called_the_new_way() -> None:
    """`max_tokens` семейство gpt-5 отвергает целиком, новое имя понимают все."""
    server = _Recorder((200, FINE))

    await _provider(server).complete(_asked())

    sent = server.sent[0]
    assert sent["max_completion_tokens"] == 200
    assert "max_tokens" not in sent
    assert "usage" not in sent, "это поле OpenRouter, OpenAI такое отвергает"


async def test_reasoning_goes_out_only_when_asked() -> None:
    """Глубина рассуждения — из профиля; нет её там, нет и в запросе."""
    server = _Recorder((200, FINE))
    provider = _provider(server)

    await provider.complete(_asked(reasoning="none"))
    await provider.complete(_asked())

    assert server.sent[0]["reasoning_effort"] == "none"
    assert "reasoning_effort" not in server.sent[1]


async def test_missing_temperature_is_not_sent() -> None:
    """``null`` в конфиге значит «не слать», а не «слать ноль»."""
    server = _Recorder((200, FINE))

    await _provider(server).complete(_asked(temperature=None))

    assert "temperature" not in server.sent[0]


# --- учимся на отказах -------------------------------------------------------


async def test_refused_temperature_is_dropped_and_remembered() -> None:
    """gpt-5 не принимает температуру: повторяем без неё и больше не шлём."""
    server = _Recorder((400, NO_TEMPERATURE), (200, FINE))
    provider = _provider(server)

    first = await provider.complete(_asked(model="gpt-5"))
    await provider.complete(_asked(model="gpt-5"))

    assert first.text == "готово"
    assert "temperature" in server.sent[0]
    assert "temperature" not in server.sent[1], "повтор ушёл с тем же параметром"
    assert "temperature" not in server.sent[2], "выученное забылось"
    assert len(server.sent) == 3, "второй запрос не должен снова спотыкаться"


async def test_named_reasoning_value_is_taken_from_the_refusal() -> None:
    """Модель сама перечисляет допустимые значения — берём наименьшее.

    Без явной глубины gpt-5-nano потратил весь потолок ответа на рассуждение и
    инструмента не выбрал; поэтому глубину не выбрасывают, а исправляют.
    """
    server = _Recorder((400, NO_NONE), (200, FINE))

    await _provider(server).complete(_asked(model="gpt-5-nano", reasoning="none"))

    assert server.sent[1]["reasoning_effort"] == "minimal"


async def test_model_without_reasoning_gets_none_of_it() -> None:
    """gpt-4.1 глубины рассуждения не знает вовсе — параметр выбрасывается."""
    server = _Recorder((400, NO_REASONING), (200, FINE))

    await _provider(server).complete(_asked(model="gpt-4.1-nano", reasoning="none"))

    assert "reasoning_effort" not in server.sent[1]


async def test_lessons_belong_to_their_model() -> None:
    """Выученное про gpt-5 не должно портить запросы к gpt-5.4-nano."""
    server = _Recorder((400, NO_TEMPERATURE), (200, FINE))
    provider = _provider(server)

    await provider.complete(_asked(model="gpt-5"))
    await provider.complete(_asked(model="gpt-5.4-nano"))

    assert server.sent[-1]["temperature"] == 0.0


async def test_two_refusals_in_a_row_are_both_fixed() -> None:
    """Модель, отвергающая и температуру, и глубину, требует двух повторов."""
    server = _Recorder((400, NO_TEMPERATURE), (400, NO_NONE), (200, FINE))

    answer = await _provider(server).complete(_asked(model="gpt-5-mini", reasoning="none"))

    assert answer.text == "готово"
    last = server.sent[-1]
    assert "temperature" not in last and last["reasoning_effort"] == "minimal"


async def test_unfixable_refusal_is_not_retried() -> None:
    """Непоправимый отказ — ошибка сразу, без круга впустую."""
    wrong = {"error": {"message": "Invalid model", "param": "model", "code": None}}
    server = _Recorder((400, wrong))

    with pytest.raises(LLMError):
        await _provider(server).complete(_asked())

    assert len(server.sent) == 1


async def test_same_refusal_twice_does_not_loop() -> None:
    """Сервер упрямо отвергает уже исправленное — сдаёмся, а не крутимся."""
    server = _Recorder((400, NO_NONE))

    with pytest.raises(LLMError):
        await _provider(server).complete(_asked(model="gpt-5-nano", reasoning="none"))

    assert len(server.sent) == 2


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        (NO_TEMPERATURE["error"], ("temperature", None)),
        (NO_NONE["error"], ("reasoning_effort", "minimal")),
        (NO_REASONING["error"], ("reasoning_effort", None)),
        ({"message": "Invalid model", "param": "model"}, None),
        ({}, None),
    ],
)
def test_refusal_is_read(detail, expected) -> None:
    """Разбор отказа по тексту, снятому с живого ключа."""
    assert lesson(detail) == expected


# --- деньги ------------------------------------------------------------------


async def test_empty_account_is_its_own_failure() -> None:
    """Кончилась квота — это не «слишком часто», хотя код ответа тот же, 429."""
    server = _Recorder((429, BROKE))

    with pytest.raises(LLMOutOfCredits) as caught:
        await _provider(server).complete(_asked())

    assert caught.value.provider == "OpenAI", "назвать надо тот счёт, что пуст"
    assert "деньги" in str(caught.value)


async def test_rate_limit_is_not_about_money() -> None:
    """Частота запросов деньгами не объясняется."""
    server = _Recorder((429, TOO_FAST))

    with pytest.raises(LLMError) as caught:
        await _provider(server).complete(_asked())

    assert not isinstance(caught.value, LLMOutOfCredits)


# --- сборка ------------------------------------------------------------------


def test_config_type_builds_the_right_provider() -> None:
    """`type: openai` в конфиге даёт этого провайдера, а не заглушку."""
    provider = build_provider(
        ProviderConfig(name="openai", type="openai", api_key="test-key")
    )

    assert isinstance(provider, OpenAIProvider)


def test_shipped_reasoning_models_say_how_deep_to_think() -> None:
    """Каждый профиль на рассуждающей модели обязан назвать глубину.

    Замер 13.09.2026: gpt-5-nano без явной глубины потратил весь потолок ответа
    на рассуждение и не выбрал инструмент. Провайдер умеет поправить отвергнутое
    значение, но не умеет заметить, что его забыли: молчание для модели значит
    «думай сколько хочешь», и запрос проходит — просто без ответа.
    """
    from jarvis.core.config import load_config

    config = load_config()
    kinds = {name: provider.type for name, provider in config.llm.providers.items()}
    forgot = [
        f"{task} ({profile.model})"
        for task, profile in config.llm.profiles.items()
        if kinds.get(profile.provider) == "openai"
        and profile.model.startswith(("gpt-5", "o1", "o3", "o4"))
        and not profile.reasoning
    ]

    assert not forgot, f"глубина рассуждения не задана: {', '.join(forgot)}"


def test_without_a_key_it_is_a_stub() -> None:
    """Нет ключа — заглушка: приложение обязано стартовать и без сети."""
    provider = build_provider(ProviderConfig(name="openai", type="openai", api_key=""))

    assert not isinstance(provider, OpenAIProvider)
