"""Окно лога показывает то же, что консоль: без отладочных строк.

Просьба владельца 14.09.2026: «Показать лог» в трее топил нужное в DEBUG.
"""

from __future__ import annotations

from jarvis.core.logging.visible import console_view, visible_levels

LOG = (
    "14.09.26, 09:42:19 DEBUG    jarvis.core.bus.local        Событие telegram.message.received без подписчиков\n"
    "14.09.26, 09:42:20 INFO     jarvis.core.voice.pipeline   Распознано: 'как дела'\n"
    "14.09.26, 09:42:21 DEBUG    jarvis.core.llm.service      LLM запрос: задача=intent\n"
    "  продолжение отладочной записи\n"
    "14.09.26, 09:42:22 ERROR    jarvis.skills.windows        Не удалось\n"
    "Traceback (most recent call last):\n"
    "  File \"x.py\", line 1\n"
)


def test_levels_start_from_console_level() -> None:
    assert visible_levels("INFO") == ("INFO", "WARNING", "ERROR", "CRITICAL")
    assert visible_levels("warning")[0] == "WARNING"
    assert visible_levels("опечатка")[0] == "INFO"


def test_debug_lines_and_their_continuations_are_hidden() -> None:
    shown = console_view(LOG, "INFO")
    assert "DEBUG" not in shown
    assert "продолжение отладочной записи" not in shown
    assert "Распознано: 'как дела'" in shown
    # Стек ошибки принадлежит записи ERROR и показывается целиком.
    assert "Traceback" in shown and 'File "x.py"' in shown


def test_debug_console_level_shows_everything() -> None:
    assert console_view(LOG, "DEBUG") == LOG
