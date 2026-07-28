"""Провайдер OpenRouter.

Реализует единственный метод контракта — `complete`. Всё остальное (суммаризация,
разбор намерений, выбор модели под задачу) живёт в `LLMService` и работает
одинаково с любым провайдером.

Идентификаторы моделей задаются в конфиге; актуальный список — на
https://openrouter.ai/models
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from jarvis.core.config import ProviderConfig
from jarvis.core.errors import LLMError, LLMNotConfigured

from ..protocol import LLMRequest, LLMResponse, ToolCall

logger = logging.getLogger(__name__)


class OpenRouterProvider:
    """Клиент OpenRouter поверх httpx."""

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    @property
    def name(self) -> str:
        """Имя провайдера из конфига."""
        return self._config.name

    @property
    def configured(self) -> bool:
        """Есть ли API-ключ."""
        return self._config.configured

    def _http(self) -> httpx.AsyncClient:
        """Ленивая инициализация HTTP-клиента."""
        if self._client is None:
            if not self.configured:
                raise LLMNotConfigured(
                    f"Провайдер {self.name!r} без API-ключа. "
                    f"Задай JARVIS_OPENROUTER_KEY в .env"
                )
            headers = {
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
                **dict(self._config.headers),
            }
            self._client = httpx.AsyncClient(
                base_url=self._config.base_url,
                headers=headers,
                timeout=self._config.timeout,
            )
        return self._client

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Отправить запрос и разобрать ответ."""
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [message.as_dict() for message in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.tools:
            payload["tools"] = list(request.tools)
            payload["tool_choice"] = request.tool_choice

        try:
            response = await self._http().post("/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:400]
            raise LLMError(f"OpenRouter вернул {exc.response.status_code}: {detail}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Сеть недоступна при обращении к OpenRouter: {exc}") from exc
        except ValueError as exc:
            raise LLMError(f"OpenRouter вернул не-JSON: {exc}") from exc

        return self._parse(data)

    def _parse(self, data: dict[str, Any]) -> LLMResponse:
        """Превратить ответ OpenRouter в `LLMResponse`."""
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"OpenRouter вернул ответ без choices: {str(data)[:200]}")

        choice = choices[0]
        message = choice.get("message") or {}

        calls: list[ToolCall] = []
        for raw_call in message.get("tool_calls") or []:
            function = raw_call.get("function") or {}
            raw_arguments = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments)
            except json.JSONDecodeError:
                logger.warning("Не удалось разобрать аргументы вызова: %r", raw_arguments)
                arguments = {}
            calls.append(
                ToolCall(
                    name=function.get("name", ""),
                    arguments=arguments,
                    call_id=raw_call.get("id", ""),
                )
            )

        return LLMResponse(
            text=(message.get("content") or "").strip(),
            tool_calls=tuple(calls),
            model=data.get("model", ""),
            finish_reason=choice.get("finish_reason", ""),
            usage=data.get("usage") or {},
        )

    async def aclose(self) -> None:
        """Закрыть HTTP-клиент."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
