"""Клавиатурный наблюдатель: буфер, совпадение фраз и пропуск чужих окон.

Хук клавиатуры — тонкая обёртка над WinAPI и проверяется живьём, как захват у
скилла `windows`. Здесь проверяется всё, что можно посчитать без ОС: как копится
буфер, как в нём находится триггер, как работает пауза и как отсекаются окна, где
следить нельзя. На этой логике и держится обещание «реагируем только на свои
фразы, остальное забываем».
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent


def _load() -> Any:
    """Загрузить скилл как модуль: он плагин и лежит вне пакета."""
    path = _ROOT / "skills" / "keys" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_keys", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


keys = _load()


def _type(triggers: Any, text: str, *, now: float = 0.0) -> list[str]:
    """Напечатать строку посимвольно, собрав всё, что сработало."""
    fired: list[str] = []
    for char in text:
        command = triggers.feed(char, now=now)
        if command is not None:
            fired.append(command)
    return fired


# --- совпадение фраз --------------------------------------------------------


def test_typed_phrase_fires_the_command() -> None:
    """Набранная фраза отдаёт свою команду — и до всякого Enter."""
    triggers = keys.Triggers({"курс рубля": "курс рубля"})
    assert _type(triggers, "курс рубля") == ["курс рубля"]


def test_command_may_differ_from_the_typed_phrase() -> None:
    """Печатают одно, роутеру уходит другое: фраза — ключ, команда — значение."""
    triggers = keys.Triggers({"погода": "какая погода в москве"})
    assert _type(triggers, "погода") == ["какая погода в москве"]


def test_nothing_fires_without_a_trigger() -> None:
    """Обычный набор проходит сквозь буфер и ничего не будит."""
    triggers = keys.Triggers({"курс рубля": "курс рубля"})
    assert _type(triggers, "привет как дела") == []


def test_trigger_found_inside_a_longer_line() -> None:
    """Фраза срабатывает и посреди набранного, а не только с начала строки."""
    triggers = keys.Triggers({"курс рубля": "курс рубля"})
    assert _type(triggers, "смотрю курс рубля") == ["курс рубля"]


def test_case_is_ignored() -> None:
    """Регистр не важен: печатают как придётся."""
    triggers = keys.Triggers({"курс рубля": "курс рубля"})
    assert _type(triggers, "КУРС Рубля") == ["курс рубля"]


def test_longer_phrase_wins_at_the_same_tail() -> None:
    """Когда на одном хвосте совпадают обе фразы, берётся длинная.

    «рубля» и «курс рубля» дочерчиваются на одном и том же последнем символе.
    Побеждать должна длинная — иначе точный триггер не срабатывает никогда.

    Оговорка: это работает для суффиксов, а не префиксов. Если один триггер —
    **начало** другого («курс» и «курс рубля»), короткий сложится раньше и
    выстрелит первым: набор идёт слева направо, ждать продолжения нечем.
    Поэтому в конфиге триггеры началом друг друга делать не стоит.
    """
    triggers = keys.Triggers({"рубля": "рубля", "курс рубля": "курс рубля"})
    assert _type(triggers, "курс рубля") == ["курс рубля"]


# --- пауза после срабатывания -----------------------------------------------


def test_same_trigger_is_muted_for_a_while() -> None:
    """Повтор той же фразы в паузе молчит: удержанная клавиша не строчит."""
    triggers = keys.Triggers({"пауза": "пауза"}, cooldown_s=4.0)
    assert triggers.feed_word("пауза", now=1.0) == "пауза"
    assert triggers.feed_word("пауза", now=2.0) is None
    # Пауза прошла — снова можно.
    assert triggers.feed_word("пауза", now=10.0) == "пауза"


# --- Backspace и Enter ------------------------------------------------------


def test_backspace_edits_the_buffer() -> None:
    """Стирание правит и наш буфер: «курр»→backspace→«курс рубля»."""
    triggers = keys.Triggers({"курс рубля": "курс рубля"})
    fired: list[str] = []
    for char in "курр":
        triggers.feed(char, now=0.0)
    triggers.backspace()  # убрали лишнюю «р»
    for char in "с рубля":
        got = triggers.feed(char, now=0.0)
        if got:
            fired.append(got)
    assert fired == ["курс рубля"]


def test_reset_forgets_the_line() -> None:
    """Сброс (это делает Enter) очищает буфер: половина фразы не достреливает."""
    triggers = keys.Triggers({"курс рубля": "курс рубля"})
    for char in "курс ":
        triggers.feed(char, now=0.0)
    triggers.reset()
    assert _type(triggers, "рубля") == []


# --- пропуск чужих окон -----------------------------------------------------


def test_sensitive_window_detected_by_title() -> None:
    """Банк и менеджер паролей узнаются по заголовку окна."""
    skip = keys.DEFAULT_SKIP
    assert keys.is_sensitive("Bitwarden — Chrome", skip)
    assert keys.is_sensitive("Сбербанк Онлайн", skip)
    assert keys.is_sensitive("Ввод пароля", skip)


def test_ordinary_window_is_not_sensitive() -> None:
    """Обычное окно следить не запрещает."""
    assert not keys.is_sensitive("YouTube — Яндекс Браузер", keys.DEFAULT_SKIP)
    assert not keys.is_sensitive("", keys.DEFAULT_SKIP)


# --- настройки --------------------------------------------------------------


def test_shipped_config_ships_the_keys_section() -> None:
    """Без секции keys наблюдатель молча остаётся с пустыми настройками.

    Проверяется рабочий конфиг: забыть блок при переносе — самый вероятный
    способ незаметно потерять фичу.
    """
    from jarvis.core.config import load_config

    config = load_config(_ROOT / "config" / "config.yaml")
    section = config.skills.settings.get("keys", {})
    assert "triggers" in section
    assert section.get("enabled") is False


def test_default_off() -> None:
    """По умолчанию — выключено: кейлоггер не должен включаться сам собой."""
    from jarvis.core.config import load_config

    config = load_config(_ROOT / "config" / "config.yaml")
    assert config.skills.settings["keys"]["enabled"] is False
