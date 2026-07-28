"""Слой LLM: тонкие провайдеры, сервис задач, профили моделей."""

from .profiles import ProfileRegistry
from .protocol import LLMProvider, LLMRequest, LLMResponse, Message, ToolCall
from .providers import NullProvider, OpenRouterProvider, build_provider
from .service import LLMService

__all__ = [
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LLMService",
    "Message",
    "NullProvider",
    "OpenRouterProvider",
    "ProfileRegistry",
    "ToolCall",
    "build_provider",
]
