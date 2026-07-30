"""Результат вызова инструмента.

Инструменты не бросают исключения наружу: ошибка — это тоже результат.
Так вызывающая сторона (роутер, другой скилл, LLM) обрабатывает успех и сбой
одинаково, а один кривой скилл не роняет ядро.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

#: Реплика инструмента. Четыре вида, от простого к полному:
#:
#: * ``"Готово."`` — одна строка на всех;
#: * ``("Пауза.", "Остановил.")`` — несколько равноправных вариантов;
#: * ``{"ru": "Пауза.", "en": "Paused."}`` — по языку вопроса;
#: * ``{"ru": ("Пауза.", "Остановил."), "en": ("Paused.",)}`` — и то, и другое.
#:
#: Варианты нужны там, где реплика звучит десятки раз в день: одна зашитая
#: строка за неделю превращается в сигнал будильника, её перестают слышать.
#: Кто именно выберет вариант — не дело инструмента: этим занята персона,
#: она же помнит, что уже говорила, и не повторяется.
Speakable = str | Sequence[str] | Mapping[str, str | Sequence[str]] | None


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

    def speech_options(
        self, language: str | None, *, fallback: str = "ru"
    ) -> tuple[str, ...]:
        """Все варианты реплики под язык вопроса.

        Выбирать из них — забота того, у кого есть персона: она помнит, что
        уже говорила. Здесь только разбор четырёх видов :data:`Speakable`.
        """
        if self.speech is None:
            return ()
        if isinstance(self.speech, Mapping):
            code = (language or fallback).split("-")[0].lower()
            for key in (code, fallback):
                if key in self.speech:
                    return _lines(self.speech[key])
            return _lines(next(iter(self.speech.values()), None))
        return _lines(self.speech)

    def speech_for(self, language: str | None, *, fallback: str = "ru") -> str | None:
        """Первый вариант реплики под язык вопроса.

        Годится там, где персоны под рукой нет: текстовый ввод, отладочный
        вывод, тесты. Голосовой конвейер берёт весь набор и выбирает сам.
        """
        options = self.speech_options(language, fallback=fallback)
        return options[0] if options else None

    def unwrap(self) -> Any:
        """Вернуть значение или бросить исключение — для внутреннего кода ядра."""
        if not self.ok:
            from jarvis.core.errors import ToolError

            raise ToolError(self.error or f"Инструмент {self.tool!r} завершился ошибкой")
        return self.value


def _lines(value: Any) -> tuple[str, ...]:
    """Привести реплику к набору непустых строк.

    Строка — это один вариант, а не последовательность символов, поэтому
    проверяется первой: иначе «Готово.» распалось бы на буквы.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, Sequence):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()
