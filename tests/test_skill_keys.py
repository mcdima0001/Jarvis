"""Клавиатурный наблюдатель: буфер, совпадение фраз и пропуск чужих окон.

Хук клавиатуры — тонкая обёртка над WinAPI и проверяется живьём, как захват у
скилла `windows`. Здесь проверяется всё, что можно посчитать без ОС: как копится
буфер, как в нём находится триггер, как работает пауза и как отсекаются окна, где
следить нельзя. На этой логике и держится обещание «реагируем только на свои
фразы, остальное забываем».
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

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


def test_reaction_fires_when_the_word_ends() -> None:
    """Реакция срабатывает на слово из списка — когда слово закончено."""
    react = keys.Reactions({"не работает": ("Как всегда.",)})
    quips = [r.quip for r in (react.feed(ch, now=0.0) for ch in "опять не работает.") if r]
    assert quips == ["Как всегда."]


def test_reaction_waits_for_the_end_of_the_word() -> None:
    """Пока слово не закончено, реакции нет: «баг» — не начало «багаж»."""
    react = keys.Reactions({"баг": ("Это не баг, сэр.",)}, cooldown_s=0.0)
    assert [r for r in (react.feed(ch, now=0.0) for ch in "багаж ") if r] == []
    assert react.finish(now=0.0) is None


@pytest.mark.parametrize("typed", ["доработает ", "дебаг ", "отработает."])
def test_reaction_needs_the_start_of_the_word(typed: str) -> None:
    """«Доработает» — не «работает», «дебаг» — не «баг» (живой запуск 14.09.2026)."""
    react = keys.Reactions({"работает": ("Не трогайте, сэр.",), "баг": ("Это не баг, сэр.",)}, cooldown_s=0.0)
    assert [r for r in (react.feed(ch, now=0.0) for ch in typed) if r] == []


@pytest.mark.parametrize("typed", ["не очень работает ", "не особо работает.", "ни разу не работает "])
def test_negated_word_does_not_get_a_happy_reaction(typed: str) -> None:
    react = keys.Reactions({"работает": ("Вот и славно.",)}, cooldown_s=0.0)
    assert [r for r in (react.feed(ch, now=0.0) for ch in typed) if r] == []


def test_negation_is_limited_to_its_own_clause() -> None:
    react = keys.Reactions({"работает": ("Вот и славно.",)}, cooldown_s=0.0)
    assert react.feed_pattern("не спал, но работает", now=0.0) is not None


def test_explicit_negative_reaction_still_wins() -> None:
    react = keys.Reactions({"работает": ("Вот и славно.",), "не работает": ("Как всегда, сэр.",)}, cooldown_s=0.0)
    result = react.feed_pattern("опять не работает", now=0.0)
    assert result is not None and result.keyword == "не работает"


def test_enter_finishes_the_last_word() -> None:
    """Слово в самом конце строки заканчивает Enter — реакция всё равно звучит."""
    react = keys.Reactions({"баг": ("Это не баг, сэр.",)})
    for ch in "опять баг":
        assert react.feed(ch, now=0.0) is None
    ending = react.finish(now=0.0)
    assert ending is not None and ending.keyword == "баг"


def test_trigger_needs_the_start_of_the_word() -> None:
    """Команда тоже только с начала слова, но конца слова не ждёт."""
    triggers = keys.Triggers({"курс рубля": "курс рубля"})
    assert _type(triggers, "перекурс рубля") == []
    assert _type(triggers, "(курс рубля") == ["курс рубля"]


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


# --- реплики, сочинённые моделью впрок ---------------------------------------


def test_written_line_joins_the_rotation() -> None:
    """Сочинённое моделью становится в общий круг с заданным в конфиге."""
    react = keys.Reactions({"баг": ("Это не баг, сэр.",)}, cooldown_s=0.0)
    assert react.learn("баг", "Занятно, сэр.")
    assert react.options("баг") == ("Это не баг, сэр.", "Занятно, сэр.")
    quips = [react.feed_pattern("баг", now=0.0).quip for _ in range(2)]  # type: ignore[union-attr]
    assert quips == ["Это не баг, сэр.", "Занятно, сэр."]


def test_the_same_line_is_not_learned_twice() -> None:
    """Повтор не копится: иначе круг выродился бы в одну реплику."""
    react = keys.Reactions({"баг": ("Это не баг, сэр.",)})
    assert react.learn("баг", "Занятно, сэр.")
    assert not react.learn("баг", "  Занятно,   сэр. ")
    assert not react.learn("баг", "Это не баг, сэр."), "повторили реплику из конфига"
    assert len(react.options("баг")) == 2


def test_unknown_word_is_not_learned() -> None:
    """Реплика на слово, за которым не следят, никому не нужна."""
    react = keys.Reactions({"баг": ("Это не баг, сэр.",)})
    assert not react.learn("дедлайн", "Оптимистично, сэр.")
    assert not react.learn("баг", "   ")


def test_owner_list_survives_the_model() -> None:
    """Вытесняется только сочинённое: список владельца — то, чему он доверяет."""
    react = keys.Reactions({"баг": ("Это не баг, сэр.",)})
    for number in range(keys.LEARNED_PER_WORD + 3):
        react.learn("баг", f"Реплика {number}.")
    options = react.options("баг")
    assert options[0] == "Это не баг, сэр."
    assert len(options) == 1 + keys.LEARNED_PER_WORD
    assert options[-1] == f"Реплика {keys.LEARNED_PER_WORD + 2}."


def test_written_line_is_asked_about_the_word_not_the_sentence() -> None:
    """Реплика пишется на слово: звучать она будет в другой раз и в другом месте."""
    prompt = keys._REACT_PROMPT.format(keyword="дедлайн", context="опять дедлайн")
    assert "дедлайн" in prompt
    assert "в любой раз" in prompt, "модель просят ответить на конкретную фразу"


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


# --- раскладка --------------------------------------------------------------


def _layout(text: str, guard: Any = None, *, now: float = 0.0) -> list[str]:
    """Напечатать строку и собрать, что заметил сторож раскладки."""
    guard = guard or keys.LayoutGuard(keys.LayoutModel.load())
    fired = [got for char in text if (got := guard.feed(char, now=now))]
    ending = guard.finish(now=now)
    return fired + ([ending] if ending else [])


def test_layout_model_ships_with_the_skill() -> None:
    assert keys.LayoutModel.load() is not None, "layout_model.json рядом со скиллом"


@pytest.mark.parametrize(
    ("word", "direction"),
    [("ghbdtn", "ru"), ("yfgbib", "ru"), ("cjcnjzybt", "ru"), ("руддщ", "en"), ("цщкл", "en"),
     ("hello", None), ("привет", None), ("json", None), ("localhost", None), ("github", None)],
)
def test_word_in_the_wrong_layout(word: str, direction: str | None) -> None:
    assert keys.LayoutModel.load().wrong_layout(word) == direction


def test_two_words_in_a_row_are_noticed() -> None:
    # «привет как дела»: «как» короткое, не считается и не сбивает.
    assert _layout("ghbdtn rfr ltkf") == ["ru"]
    assert _layout("руддщ цщкдв") == ["en"]


def test_one_word_is_not_enough() -> None:
    assert _layout("ghbdtn hello world") == []


def test_ordinary_text_stays_quiet() -> None:
    assert _layout("please check this function again before release") == []
    assert _layout("привет, сегодня будем чинить раскладку и эквалайзер") == []


def test_layout_remark_repeats_only_after_a_switch() -> None:
    """Пока пишут не той раскладкой — одно замечание; переключился и снова ошибся — снова."""
    guard = keys.LayoutGuard(keys.LayoutModel.load())
    fired = [got for char in "ghbdtn ltkf rfr ltkf ltkftim " if (got := guard.feed(char))]
    assert fired == ["ru"]
    fired = [got for char in "привет ghbdtn ltkf " if (got := guard.feed(char))]
    assert fired == ["ru"], "слово в верной раскладке снова взводит"
    assert guard.finish() is None
    fired = [got for char in "ghbdtn ltkf " if (got := guard.feed(char))]
    assert fired == ["ru"], "Enter — новое сообщение"


def test_short_common_words_count_too() -> None:
    """«Rfr ltkf& Xnj ltkftim&» — «Как дела? Что делаешь?» (живой набор 15.09.2026)."""
    assert _layout("Rfr ltkf& Xnj ltkftim&") == ["ru"]
    assert _layout("ye lf") == ["ru"], "ну да"
    assert _layout("еру фтв") == ["en"], "the and"


def test_short_words_typed_right_stay_quiet() -> None:
    assert _layout("is it ok to do so") == []
    assert _layout("ну да, как дела, что там") == []


def test_dropped_short_words_do_not_collide_with_english() -> None:
    """«мы» (vs), «че» (xt), «ща» (of) в латинице — обычные английские токены."""
    assert "vs" not in keys._SHORT_WRONG and "of" not in keys._SHORT_WRONG and "xt" not in keys._SHORT_WRONG


# --- исправление раскладки ----------------------------------------------------


def _typed(guard: Any, text: str) -> list[str]:
    return [got for char in text if (got := guard.feed(char))]


def test_swap_layout_keeps_case_and_shifted_signs() -> None:
    assert keys.swap_layout("Rfr ltkf& Xnj ltkftim&", "ru") == "Как дела? Что делаешь?"
    assert keys.swap_layout("руддщ цщкдв", "en") == "hello world"
    assert keys.swap_layout(keys.swap_layout("Привет, 42!", "en"), "ru") == "Привет, 42!"


def test_fix_covers_only_text_after_the_last_right_word() -> None:
    """Просьба владельца 17.09.2026: сказать и заменить текст на тот же в верной раскладке."""
    guard = keys.LayoutGuard(keys.LayoutModel.load())
    assert _typed(guard, "hello ghbdtn rfr ") == ["ru"]
    assert guard.pending_fix() == ("ru", "ghbdtn rfr ")
    guard.fixed(keys.swap_layout("ghbdtn rfr ", "ru"))
    assert guard.pending_fix() is None


def test_fix_includes_what_was_typed_before_it_ran() -> None:
    """Правка делается между событиями: успевшее набраться после срабатывания тоже чинится."""
    guard = keys.LayoutGuard(keys.LayoutModel.load())
    _typed(guard, "Rfr ltkf& Xn")
    assert guard.pending_fix() == ("ru", "Rfr ltkf& Xn")


def test_fix_follows_backspace_and_is_dropped_when_the_cursor_moves() -> None:
    guard = keys.LayoutGuard(keys.LayoutModel.load())
    _typed(guard, "ghbdtn ltkf x")
    guard.backspace()
    assert guard.pending_fix() == ("ru", "ghbdtn ltkf ")
    guard.break_line()
    assert guard.pending_fix() is None
    _typed(guard, "ghbdtn ltkf ")
    assert guard.finish() is None and guard.pending_fix() is None, "после Enter не правим"


def test_layout_remark_says_it_fixed() -> None:
    skill = keys.KeysSkill()
    said: list[str] = []
    skill._layout_quips = dict(keys.DEFAULT_LAYOUT_QUIPS)
    skill._layout_turn = 0
    skill._context = SimpleNamespace(  # type: ignore[assignment]
        announcer=SimpleNamespace(offer=lambda text, **_: said.append(text)),
        modes=SimpleNamespace(active=lambda mode: False),
        logger=logging.getLogger("test.keys"),
    )
    skill._on_layout("ru", True)
    assert said[-1].endswith(keys.LAYOUT_FIXED)


def test_reactions_survive_restart() -> None:
    """Жалоба 19.09.2026: после перезапуска на «работает» снова первая реплика, сочинённое пропадало."""
    first = keys.Reactions({"работает": ("Раз.", "Два.")}, cooldown_s=0)
    assert first.feed_pattern("работает ").quip == "Раз."
    assert first.learn("работает", "Сочинил сам.")
    saved = first.snapshot()

    second = keys.Reactions({"работает": ("Раз.", "Два.")}, cooldown_s=0)
    second.restore(saved)
    assert second.feed_pattern("работает ").quip == "Два.", "круг продолжается, а не начинается заново"
    assert "Сочинил сам." in second.options("работает")
    second.restore({"learned": {"удалённое слово": ["x"]}, "turn": {"удалённое слово": 3}})
    assert "удалённое слово" not in second.snapshot()["learned"]


def test_games_switch_the_watcher_off() -> None:
    """19.09.2026: в Minecraft прозвучало «раскладка не та» — WASD и чат не текст."""
    games = keys.DEFAULT_GAMES
    assert keys.is_game(r"C:\Program Files\Java\bin\javaw.exe", False, games)
    assert keys.is_game(r"D:\SteamLibrary\steamapps\common\Game\game.exe", False, games)
    assert keys.is_game(r"C:\Windows\explorer.exe", True, games), "полноэкранный Direct3D — игра"
    assert not keys.is_game(r"C:\Program Files\Telegram\Telegram.exe", False, games)
