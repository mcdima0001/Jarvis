"""Резолверы — звенья цепочки маршрутизации.

Порядок задаётся в конфиге (``router.resolvers``). Добавить свой способ разбора
(эмбеддинги, грамматики, внешний NLU) — значит написать класс с методом
`resolve` и вписать его в `RESOLVERS`.
"""

from .alias import AliasResolver
from .fallback import CHAT_TOOL, FallbackResolver
from .llm import LLMResolver
from .phrase import PhraseResolver

__all__ = [
    "CHAT_TOOL",
    "AliasResolver",
    "FallbackResolver",
    "LLMResolver",
    "PhraseResolver",
]
