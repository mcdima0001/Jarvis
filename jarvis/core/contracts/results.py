"""Результат вызова инструмента.

Инструменты не бросают исключения наружу: ошибка — это тоже результат.
Так вызывающая сторона (роутер, другой скилл, LLM) обрабатывает успех и сбой
одинаково, а один кривой скилл не роняет ядро.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

#: Реплика: одна строка или варианты по языкам — ``{"ru": "...", "en": "..."}``.
Speakable = str | Mapping[str, str] | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolResult:
    """Итог работы инструмента.

    :param ok: успешно ли отработал инструмент.
    :param value: полезная нагрузка (число, словарь, что угодно сериализуемое).
    :param error: текст ошибки, если ``ok`` ложно.
    :param speech: что произнести пользователю. Либо строка, либо варианты по
        языкам — тогда ответ прозвучит на языке вопроса.
    """

    ok: bool
    value: Any = None
    error: str | None = None
    tool: str = ""
    duration: float = 0.0
    speech: Speakable = None

    @classmethod
    def success(
        cls,
        value: Any = None,
        *,
        tool: str = "",
        speech: Speakable = None,
        duration: float = 0.0,
    ) -> "ToolResult":
        """Удачный результат."""
        return cls(ok=True, value=value, tool=tool, speech=speech, duration=duration)

    @classmethod
    def failure(
        cls,
        error: str,
        *,
        tool: str = "",
        speech: Speakable = None,
        duration: float = 0.0,
    ) -> "ToolResult":
        """Неудачный результат с описанием причины."""
        return cls(ok=False, error=error, tool=tool, speech=speech, duration=duration)

    def speech_for(self, language: str | None, *, fallback: str = "ru") -> str | None:
        """Выбрать реплику под язык вопроса.

        Если скилл задал одну строку, она и вернётся: не всякая реплика
        нуждается в переводе.
        """
        if self.speech is None or isinstance(self.speech, str):
            return self.speech

        code = (language or fallback).split("-")[0].lower()
        return (
            self.speech.get(code)
            or self.speech.get(fallback)
            or next(iter(self.speech.values()), None)
        )

    def unwrap(self) -> Any:
        """Вернуть значение или бросить исключение — для внутреннего кода ядра."""
        if not self.ok:
            from jarvis.core.errors import ToolError

            raise ToolError(self.error or f"Инструмент {self.tool!r} завершился ошибкой")
        return self.value
