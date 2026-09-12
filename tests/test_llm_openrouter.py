"""Провайдер OpenRouter: как разбираются ответы и, главное, отказы.

Сети тут нет — HTTP подделан. Проверяется то, что решает код.

Главное проверяемое свойство — **сбой называется своим именем**. Ночью
13.09.2026 замер скилла `photo_place` выдал шестнадцать «не узнаю» подряд, и
выглядело это провалом механизма; на деле OpenRouter отвечал «можешь позволить
себе 105 токенов из запрошенных 300». Ошибка, о которой ассистент говорит не
своими словами, стоит часов поисков не там.
"""

from __future__ import annotations

import httpx
import pytest

from jarvis.core.config import ProviderConfig
from jarvis.core.errors import LLMError, LLMNotConfigured, LLMOutOfCredits
from jarvis.core.llm.protocol import LLMRequest, Message
from jarvis.core.llm.providers.openrouter import OpenRouterProvider

#: Что OpenRouter отвечает на пустом счету — дословно.
BROKE = {
    "error": {
        "message": (
            "This request requires more credits, or fewer max_tokens. "
            "You requested up to 300 tokens, but can only afford 105."
        ),
        "code": 402,
        "metadata": {"limit_source": "openrouter_credits"},
    }
}


def _provider(handler) -> OpenRouterProvider:
    """Провайдер на поддельном HTTP."""
    config = ProviderConfig(name="openrouter", type="openrouter", api_key="test-key")
    provider = OpenRouterProvider(config)
    provider._client = httpx.AsyncClient(
        base_url="https://openrouter.test/api/v1",
        transport=httpx.MockTransport(handler),
    )
    return provider


def _asked() -> LLMRequest:
    return LLMRequest(messages=(Message.user("где это снято"),), model="test/model")


async def test_empty_account_is_its_own_failure() -> None:
    """Пустой счёт — отдельный вид ошибки, а не строка в тексте общей."""
    provider = _provider(lambda request: httpx.Response(402, json=BROKE))

    with pytest.raises(LLMOutOfCredits) as caught:
        await provider.complete(_asked())

    assert "деньги" in str(caught.value), "владельцу должно быть понятно без словаря"
    assert isinstance(caught.value, LLMError), "это по-прежнему ошибка модели"


async def test_other_failures_stay_general() -> None:
    """Пятисотка провайдера деньгами не объясняется."""
    provider = _provider(lambda request: httpx.Response(500, text="боль"))

    with pytest.raises(LLMError) as caught:
        await provider.complete(_asked())

    assert not isinstance(caught.value, LLMOutOfCredits)


async def test_missing_key_is_noticed_before_any_request() -> None:
    """Без ключа запрос даже не собирается."""
    provider = OpenRouterProvider(
        ProviderConfig(name="openrouter", type="openrouter", api_key="")
    )

    with pytest.raises(LLMNotConfigured):
        await provider.complete(_asked())


async def test_answer_is_parsed() -> None:
    """Обычный ответ разбирается целиком, вместе с расходом."""
    body = {
        "model": "test/model",
        "choices": [{"message": {"content": " Анталья "}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1879, "completion_tokens": 12},
    }
    provider = _provider(lambda request: httpx.Response(200, json=body))

    answer = await provider.complete(_asked())

    assert answer.text == "Анталья"
    assert answer.usage["prompt_tokens"] == 1879
