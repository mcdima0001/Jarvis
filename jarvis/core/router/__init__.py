"""Маршрутизация: цепочка резолверов и диспетчер."""

from .dispatcher import Dispatcher
from .protocol import Resolver
from .resolvers import (
    CHAT_TOOL,
    AliasResolver,
    FallbackResolver,
    LearnedResolver,
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
    "LearnedResolver",
    "PhraseResolver",
    "Resolver",
    "Router",
]
