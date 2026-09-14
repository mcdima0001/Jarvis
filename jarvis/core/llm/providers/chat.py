"""Общий разговор по протоколу chat/completions.

OpenRouter и OpenAI говорят на одном протоколе, и расходятся ровно в трёх местах:
как называется потолок ответа, какие параметры модель вообще принимает и как
звучит отказ из-за денег. Всё остальное — HTTP, разбор ответа, вызовы
инструментов — одно и то же и написано здесь один раз. Провайдер-наследник
переопределяет `payload` и `failure`, и больше ничего.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from jarvis.core.config import ProviderConfig
from jarvis.core.errors import LLMError, LLMNotConfigured

from ..protocol import LLMRequest, LLMResponse, ToolCall

logger = logging.getLogger(__name__)


class ChatCompletionsProvider:
    """Клиент протокола chat/completions поверх httpx."""

    #: Как провайдера зовут в сообщениях об ошибках — и вслух, если дойдёт.
    title = "chat/completions"
    #: Переменная окружения с ключом: её и подсказываем, когда ключа нет.
    key_variable = ""
    #: Адрес по умолчанию, если в конфиге не задан свой.
    default_url = ""

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
                    f"Задай {self.key_variable or 'ключ'} в .env"
                )
            headers = {
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
                **dict(self._config.headers),
            }
            self._client = httpx.AsyncClient(
                base_url=self._config.base_url or self.default_url,
                headers=headers,
                timeout=self._config.timeout,
            )
        return self._client

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Отправить запрос и разобрать ответ."""
        return await self._exchange(self.payload(request))

    async def stream(self, request: LLMRequest, usage: dict[str, Any]) -> AsyncIterator[str]:
        """Ответ по мере написания — куски текста по порядку.

        Нужен разговору: ответ модели в секунду длиной начинает звучать с
        первым предложением, а не когда дописан весь (просьба владельца
        14.09.2026 «отвечать моментально»). Инструменты в потоке не
        разбираются: поток просят только там, где ждут текст.

        Расход приходит последним куском (`stream_options.include_usage`) и
        кладётся в `usage`. Отказ сервера — та же ошибка, что и у `complete`.
        """
        body = self.payload(request)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        try:
            async with self._http().stream("POST", "/chat/completions", json=body) as response:
                if response.is_error:
                    await response.aread()
                    raise self.failure(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        logger.debug("Кусок потока не JSON: %r", data[:200])
                        continue
                    if chunk.get("usage"):
                        usage.update(chunk["usage"])
                    for choice in chunk.get("choices") or []:
                        text = (choice.get("delta") or {}).get("content")
                        if text:
                            yield text
        except httpx.HTTPError as exc:
            raise LLMError(f"Сеть недоступна при обращении к {self.title}: {exc}") from exc

    def payload(self, request: LLMRequest) -> dict[str, Any]:
        """Тело запроса: то, в чём провайдеры не расходятся."""
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [message.as_dict() for message in request.messages],
        }
        if request.tools:
            body["tools"] = list(request.tools)
            body["tool_choice"] = request.tool_choice
        return body

    def failure(self, response: httpx.Response) -> LLMError:
        """Во что превратить отказ сервера. Наследник узнаёт свои особые случаи."""
        return LLMError(
            f"{self.title} вернул {response.status_code}: {response.text[:400]}"
        )

    async def _exchange(self, payload: dict[str, Any]) -> LLMResponse:
        """Один круг: запрос, ответ, разбор."""
        try:
            response = await self._http().post("/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            raise self.failure(exc.response) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Сеть недоступна при обращении к {self.title}: {exc}") from exc
        except ValueError as exc:
            raise LLMError(f"{self.title} вернул не-JSON: {exc}") from exc
        return self._parse(data)

    def _parse(self, data: dict[str, Any]) -> LLMResponse:
        """Превратить ответ в `LLMResponse`."""
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.title} вернул ответ без choices: {str(data)[:200]}")

        choice = choices[0]
        message = choice.get("message") or {}

        calls: list[ToolCall] = []
        for raw_call in message.get("tool_calls") or []:
            function = raw_call.get("function") or {}
            raw_arguments = function.get("arguments") or "{}"
            try:
                arguments = (
                    json.loads(raw_arguments)
                    if isinstance(raw_arguments, str)
                    else dict(raw_arguments)
                )
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
