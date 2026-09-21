"""Провайдер OpenAI.

Протокол тот же, что у OpenRouter (`chat.py`), но модели OpenAI **несовместимы
между собой по параметрам**, и это выяснилось замером 13.09.2026, а не из
документации:

* `max_tokens` семейство gpt-5 отвергает целиком — потолок ответа зовётся
  `max_completion_tokens`. Его понимает и старое семейство, поэтому шлём всегда.
* **Температуру** gpt-5, gpt-5-mini, gpt-5.5 не принимают вовсе (только значение
  по умолчанию), а gpt-5.4-mini и gpt-4.1 принимают.
* **Глубина рассуждения** (`reasoning_effort`) у gpt-5-nano и gpt-5-mini бывает
  не ниже `minimal`, у gpt-5.4 и новее — `none`, а gpt-4.1 такого параметра не
  знает и отвергает запрос.

Последнее не придирка. **gpt-5-nano без явной глубины потратил все двести
токенов на рассуждение и не выбрал инструмент вовсе**, а с `minimal` выбрал
верный за полторы секунды. Модель, которая думает над «сделай громче», для
голосового ассистента хуже модели, которая не умеет думать.

Поэтому глубина задаётся профилем задачи в конфиге, а провайдер **учится на
отказах**: модель, отвергнувшая параметр, говорит в ответе, какой именно и какие
значения допустимы. Провайдер запоминает это для модели и повторяет запрос
исправленным. Нужно это не для конфига — его правят один раз, — а для смены
модели на лету (`llm.set_model`): голосом переключили задачу на модель с другими
правилами, и она обязана заработать, а не отвечать ошибкой до перезапуска.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping

import httpx

from jarvis.core.errors import LLMError, LLMOutOfCredits

from ..protocol import LLMRequest, LLMResponse
from .chat import ChatCompletionsProvider

logger = logging.getLogger(__name__)

#: Что модель вправе отвергнуть и что провайдер умеет поправить сам.
ADAPTABLE = ("temperature", "reasoning_effort")

#: Чем пустой счёт отличается от обычного «слишком часто»: оба приходят как
#: 429, и разница только внутри тела отказа. Смотреть надо **и `code`, и
#: `type`**: 21.09.2026 OpenAI ответил `type: insufficient_quota` при
#: `code: credit_balance_exhausted`, проверка по одному `code` его не узнала —
#: и два дня пустой счёт звучал как «не справился, сэр».
NO_MONEY_MARKS = frozenset({"insufficient_quota", "credit_balance_exhausted", "billing_hard_limit_reached"})

#: Хвост отказа, где модель перечисляет допустимые значения.
_SUPPORTED = re.compile(r"Supported values are:\s*(.+)", re.IGNORECASE)


def lesson(detail: Mapping[str, Any]) -> tuple[str, Any] | None:
    """Чему учит отказ: какой параметр поправить и на что. ``None`` — нечему.

    Значение ``None`` означает «не слать вовсе»: так температура уходит из
    запроса к gpt-5, а глубина рассуждения — из запроса к gpt-4.1. Если модель
    назвала допустимые значения, берётся первое: в списке они идут от меньшей
    глубины к большей, а голосовому ассистенту нужна наименьшая.
    """
    message = str(detail.get("message", ""))
    param = detail.get("param")
    if param not in ADAPTABLE:
        param = next((name for name in ADAPTABLE if name in message), None)
    if param is None:
        return None
    supported = _SUPPORTED.search(message)
    if supported:
        values = re.findall(r"'([^']+)'", supported.group(1))
        if values:
            return param, values[0]
    return param, None


def _detail(response: httpx.Response) -> dict[str, Any]:
    """Поле `error` из тела отказа. Пусто — тело не JSON или поля нет."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return {}
    return error if isinstance(error, dict) else {}


class _Refused(LLMError):
    """Модель отвергла параметр, который можно поправить и повторить."""

    def __init__(self, message: str, detail: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.detail = detail


class OpenAIProvider(ChatCompletionsProvider):
    """Клиент OpenAI."""

    title = "OpenAI"
    key_variable = "JARVIS_OPENAI_KEY"
    default_url = "https://api.openai.com/v1"

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        #: Выученное из отказов: модель -> {параметр: значение, ``None`` — не слать}.
        self._learned: dict[str, dict[str, Any]] = {}

    def payload(self, request: LLMRequest) -> dict[str, Any]:
        """Тело запроса с поправками, выученными для этой модели."""
        body = super().payload(request)
        body["max_completion_tokens"] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.reasoning:
            body["reasoning_effort"] = request.reasoning
        for param, value in self._learned.get(request.model, {}).items():
            if value is None:
                body.pop(param, None)
            else:
                body[param] = value
        return body

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Отправить запрос; отвергнутый параметр поправить и повторить.

        Повторов не больше, чем параметров, которые вообще можно поправить:
        модель, отвергающая и температуру, и глубину, требует двух.
        """
        for _ in range(len(ADAPTABLE)):
            payload = self.payload(request)
            try:
                return await self._exchange(payload)
            except _Refused as refused:
                if not self._learn(request.model, payload, refused.detail):
                    raise
        return await self._exchange(self.payload(request))

    def _learn(
        self, model: str, sent: Mapping[str, Any], detail: Mapping[str, Any]
    ) -> bool:
        """Запомнить поправку. ``False`` — поправлять нечего, повтор бессмыслен."""
        found = lesson(detail)
        if found is None:
            return False
        param, value = found
        if sent.get(param) == value:
            return False
        self._learned.setdefault(model, {})[param] = value
        logger.warning(
            "Модель %s отвергла %s=%r, дальше шлю %s. Поправь профиль в конфиге, "
            "чтобы не терять на этом запрос при каждом запуске",
            model,
            param,
            sent.get(param),
            "без него" if value is None else repr(value),
        )
        return True

    def failure(self, response: httpx.Response) -> LLMError:
        """Пустой счёт и поправимый параметр — свои виды ошибки."""
        detail = _detail(response)
        message = f"OpenAI вернул {response.status_code}: {response.text[:400]}"
        marks = {str(detail.get("code") or ""), str(detail.get("type") or "")}
        if response.status_code == 429 and marks & NO_MONEY_MARKS:
            return LLMOutOfCredits(
                f"На счету OpenAI кончились деньги: {detail.get('message', '')}",
                provider=self.title,
            )
        if response.status_code == 400 and lesson(detail) is not None:
            return _Refused(message, detail)
        return super().failure(response)
