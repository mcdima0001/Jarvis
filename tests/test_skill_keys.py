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


# --- реакции (ирония на набранное) ------------------------------------------


def test_reaction_fires_on_a_substring() -> None:
    """Реакция срабатывает на подстроку в наборе, а не на команду."""
    react = keys.Reactions({"не работает": ("Как всегда.",)})
    quips = [r.quip for r in (react.feed(ch, now=0.0) for ch in "опять не работает") if r]
    assert "Как всегда." in quips


def test_reaction_carries_keyword_and_context() -> None:
    """Реакция несёт совпавшее слово и недавний набор — для модели."""
    react = keys.Reactions({"кофе": ("Одобряю.",)})
    result = react.feed_pattern("хочу кофе", now=0.0)
    assert result is not None
    assert result.keyword == "кофе"
    assert "хочу кофе" in result.context


def test_reactions_rotate_through_variants() -> None:
    """Варианты выдаются по кругу, а не один и тот же — иначе попугай."""
    react = keys.Reactions({"почему": ("А.", "Б.")}, cooldown_s=0.0)
    quips = [react.feed_pattern("почему", now=0.0).quip for _ in range(3)]  # type: ignore[union-attr]
    assert quips == ["А.", "Б.", "А."]


def test_reaction_is_muted_during_cooldown() -> None:
    """Одна и та же реакция не строчит: после срабатывания пауза."""
    react = keys.Reactions({"кофе": ("Одобряю.",)}, cooldown_s=60.0)
    assert react.feed_pattern("кофе", now=1.0).quip == "Одобряю."  # type: ignore[union-attr]
    assert react.feed_pattern("кофе", now=2.0) is None
    assert react.feed_pattern("кофе", now=100.0).quip == "Одобряю."  # type: ignore[union-attr]


def test_no_reaction_without_a_pattern() -> None:
    """Обычный текст реакций не будит."""
    react = keys.Reactions({"кофе": ("Одобряю.",)})
    assert [c for c in (react.feed(ch, now=0.0) for ch in "просто текст") if c] == []


# --- «дословно»: не переписывать --------------------------------------------


def test_verbatim_marker_is_stripped() -> None:
    """«Дословно …» просит не причёсывать и убирается из текста."""
    literal, body = keys.strip_verbatim_marker("дословно привет мир")
    assert literal
    assert body == "привет мир"


def test_no_marker_means_rewrite() -> None:
    """Без метки текст пойдёт на переписывание, метку не выдумываем."""
    literal, body = keys.strip_verbatim_marker("что работает")
    assert not literal
    assert body == "что работает"


# --- ввод текста в поле -----------------------------------------------------


def test_unicode_events_are_down_then_up() -> None:
    """Каждый символ — код-юнит, на него нажатие и отпускание."""
    events = keys.unicode_events("Ab")
    assert events == [(0x41, False), (0x41, True), (0x62, False), (0x62, True)]


def test_unicode_events_carry_cyrillic() -> None:
    """Кириллица уходит своим код-юнитом — раскладка не при чём."""
    events = keys.unicode_events("я")
    assert events == [(0x044F, False), (0x044F, True)]


def test_emoji_becomes_a_surrogate_pair() -> None:
    """Символ вне BMP — два код-юнита, иначе вставится половина."""
    events = keys.unicode_events("\U0001F600")  # 😀
    units = [unit for unit, is_up in events if not is_up]
    assert units == [0xD83D, 0xDE00]


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
    способ незаметно потерять фичу. Значение `enabled` тут не проверяем — оно
    принадлежит владельцу, он его и переключает.
    """
    from jarvis.core.config import load_config

    config = load_config(_ROOT / "config" / "config.yaml")
    section = config.skills.settings.get("keys", {})
    assert "triggers" in section
    assert "reactions" in section
    assert "enabled" in section


def test_code_default_is_off() -> None:
    """Гарантия приватности — в коде, а не в конфиге: без настройки — выключено.

    Владелец может включить кейлоггер в своём конфиге, но пустой конфиг (перенос,
    свежая машина) не должен поднимать его молча.
    """
    assert keys.DEFAULT_ENABLED is False
