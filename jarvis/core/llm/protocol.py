"""Контракт провайдера LLM — намеренно тонкий.

Провайдер умеет ровно одно: отправить запрос и вернуть ответ. Ни суммаризации,
ни разбора намерений здесь нет — иначе каждый новый провайдер (Gemini, Groq,
DeepSeek…) переписывал бы одну и ту же логику промптов. Задачи живут уровнем
выше, в `LLMService`.

**Новый провайдер = один метод `complete`.**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True, slots=True, kw_only=True)
class Message:
    """Реплика в диалоге."""

    role: str  # system | user | assistant | tool
    content: str
    #: Картинки к реплике, каждая как ``data:``-URI. Пусто почти всегда: их
    #: прикладывает только тот, кто смотрит на экран.
    images: tuple[str, ...] = ()

    @classmethod
    def system(cls, content: str) -> "Message":
        """Системная инструкция."""
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str, *, images: Sequence[str] = ()) -> "Message":
        """Реплика пользователя, при необходимости с картинками."""
        return cls(role="user", content=content, images=tuple(images))

    @classmethod
    def assistant(cls, content: str) -> "Message":
        """Реплика ассистента."""
        return cls(role="assistant", content=content)

    def as_dict(self) -> dict[str, Any]:
        """Представление для HTTP-запроса.

        **Без картинок форма не меняется вовсе**, и это главное здесь. Составной
        вид (`content` списком частей) провайдеры понимают наравне со строкой,
        но проверен он у нас только на зрении, а текстовых запросов система
        делает тысячи. Менять форму всех ради одного значило бы рисковать всем
        остальным ради возможности, которая срабатывает раз в день.
        """
        if not self.images:
            return {"role": self.role, "content": self.content}
        parts: list[dict[str, Any]] = [{"type": "text", "text": self.content}]
        parts.extend(
            {"type": "image_url", "image_url": {"url": url}} for url in self.images
        )
        return {"role": self.role, "content": parts}


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCall:
    """Вызов инструмента, который предложила модель."""

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    call_id: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMRequest:
    """Запрос к модели."""

    messages: Sequence[Message]
    model: str
    temperature: float = 0.7
    max_tokens: int = 1024
    #: Схемы инструментов для function-calling.
    tools: Sequence[Mapping[str, Any]] = ()
    #: ``auto`` | ``none`` | ``required``
    tool_choice: str = "auto"


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMResponse:
    """Ответ модели."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    model: str = ""
    finish_reason: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        """Предложила ли модель вызвать инструмент."""
        return bool(self.tool_calls)


@runtime_checkable
class LLMProvider(Protocol):
    """Транспорт до конкретного провайдера."""

    @property
    def name(self) -> str:
        """Имя провайдера из конфига."""
        ...

    @property
    def configured(self) -> bool:
        """Готов ли провайдер к работе (есть ключ и адрес)."""
        ...

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Выполнить запрос и вернуть ответ."""
        ...

    async def aclose(self) -> None:
        """Закрыть соединения."""
        ...
