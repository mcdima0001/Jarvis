"""Заглушка LLM: система стартует и тестируется без API-ключа.

Возвращает честный отказ вместо выдуманного ответа — так проще заметить, что
провайдер не настроен, чем ловить правдоподобную чушь.
"""

from __future__ import annotations

import logging

from ..protocol import LLMRequest, LLMResponse

logger = logging.getLogger(__name__)

_REPLY = "Языковая модель не настроена: добавь API-ключ в .env, чтобы я мог отвечать свободно."


class NullProvider:
    """Провайдер, который ничего никуда не отправляет."""

    def __init__(self, name: str = "null") -> None:
        self._name = name

    @property
    def name(self) -> str:
        """Имя провайдера."""
        return self._name

    @property
    def configured(self) -> bool:
        """Заглушка никогда не считается настроенной."""
        return False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Вернуть фиксированный ответ и предупредить в логе."""
        logger.warning(
            "Запрос к модели %s ушёл в заглушку — провайдер не настроен",
            request.model or "(без модели)",
        )
        return LLMResponse(text=_REPLY, model="null", finish_reason="not_configured")

    async def aclose(self) -> None:
        """Закрывать нечего."""
