"""Реализации провайдеров LLM.

Добавить Gemini, OpenAI, DeepSeek, Groq или Claude — значит написать один класс
с методом `complete` и вписать его в `build_provider`. Остальной проект не меняется.
"""

from __future__ import annotations

import logging

from jarvis.core.config import ProviderConfig

from ..protocol import LLMProvider
from .null import NullProvider
from .openrouter import OpenRouterProvider

logger = logging.getLogger(__name__)

#: Тип провайдера из конфига -> класс реализации.
PROVIDERS: dict[str, type] = {
    "openrouter": OpenRouterProvider,
    "null": NullProvider,
}


def build_provider(config: ProviderConfig) -> LLMProvider:
    """Создать провайдера по его секции конфига.

    Если тип неизвестен или ключ не задан, возвращается заглушка: приложение
    должно стартовать даже без доступа к сети.
    """
    factory = PROVIDERS.get(config.type)
    if factory is None:
        logger.warning(
            "Неизвестный тип провайдера %r — использую заглушку. Известные: %s",
            config.type,
            ", ".join(sorted(PROVIDERS)),
        )
        return NullProvider(config.name)

    if factory is NullProvider:
        return NullProvider(config.name)

    if not config.configured:
        logger.warning(
            "Провайдер %s не настроен (нет API-ключа) — использую заглушку",
            config.name,
        )
        return NullProvider(config.name)

    return factory(config)


__all__ = ["PROVIDERS", "NullProvider", "OpenRouterProvider", "build_provider"]
