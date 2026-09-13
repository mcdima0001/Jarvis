"""Провайдер OpenRouter.

Реализует единственный метод контракта — `complete`. Всё остальное (суммаризация,
разбор намерений, выбор модели под задачу) живёт в `LLMService` и работает
одинаково с любым провайдером. Протокол общий с OpenAI и живёт в `chat.py`;
здесь только то, чем OpenRouter отличается.

Идентификаторы моделей задаются в конфиге; актуальный список — на
https://openrouter.ai/models
"""

from __future__ import annotations

from typing import Any

import httpx

from jarvis.core.errors import LLMError, LLMOutOfCredits

from ..protocol import LLMRequest
from .chat import ChatCompletionsProvider

#: Код «нужно заплатить». OpenRouter отвечает им и когда счёт пуст, и когда на
#: остаток не влезает запрошенный `max_tokens` — для нас это одно и то же.
PAYMENT_REQUIRED = 402


class OpenRouterProvider(ChatCompletionsProvider):
    """Клиент OpenRouter."""

    title = "OpenRouter"
    key_variable = "JARVIS_OPENROUTER_KEY"
    default_url = "https://openrouter.ai/api/v1"

    def payload(self, request: LLMRequest) -> dict[str, Any]:
        """Потолок ответа зовётся `max_tokens`, а цену ответа просим вернуть."""
        body = super().payload(request)
        body["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        # OpenRouter вернёт не только число токенов, но и цену запроса в
        # долларах. Считать её самим — значит держать в коде прайс-лист и
        # ошибаться при каждом его изменении.
        body["usage"] = {"include": True}
        return body

    def failure(self, response: httpx.Response) -> LLMError:
        """Пустой счёт — отдельный вид ошибки."""
        if response.status_code == PAYMENT_REQUIRED:
            return LLMOutOfCredits(
                f"На счету OpenRouter кончились деньги: {response.text[:400]}",
                provider=self.title,
            )
        return super().failure(response)
