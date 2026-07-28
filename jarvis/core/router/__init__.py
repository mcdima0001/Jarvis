"""Маршрутизация: цепочка резолверов и диспетчер."""

from .dispatcher import Dispatcher
from .protocol import Resolver
from .resolvers import (
    CHAT_TOOL,
    AliasResolver,
    FallbackResolver,
    LLMResolver,
    PhraseResolver,
)
from .router import Router

__all__ = [
    "CHAT_TOOL",
    "AliasResolver",
    "Dispatcher",
    "FallbackResolver",
    "LLMResolver",
    "PhraseResolver",
    "Resolver",
    "Router",
]
