"""Манера речи: варианты реплик, обращение, отсутствие повторов."""

from __future__ import annotations

from datetime import datetime

import pytest

from jarvis.core.persona import (
    DONE,
    FAILED,
    FAREWELL,
    GREETING,
    LISTENING,
    PHRASES,
    SITUATIONS,
    Persona,
    daypart,
)
from jarvis.core.tts.normalize import normalize_for_speech


def _first(items):
    """Детерминированный выбор: всегда первый доступный вариант."""
    return items[0]


# --- наборы ---------------------------------------------------------------


def test_listening_has_at_least_ten_variants():
    """Отклик на имя звучит чаще всего — одной фразы мало.

    Требование владельца: минимум десяток вариантов, иначе ассистент
    воспринимается как будильник с одной мелодией.
    """
    persona = Persona()
    for language in ("ru", "en"):
        assert len(persona.variants(LISTENING, language)) >= 10


def test_every_situation_has_both_languages():
    """Ассистент двуязычный: молчать на английском он не должен."""
    for situation in SITUATIONS:
        for language in ("ru", "en"):
            assert PHRASES[situation][language], f"{situation}/{language} пуст"


def test_no_duplicate_phrases():
    """Копия внутри набора уменьшает разнообразие незаметно для глаза."""
    for situation, languages in PHRASES.items():
        for language, lines in languages.items():
            assert len(set(lines)) == len(lines), f"повтор в {situation}/{language}"


def test_phrases_survive_speech_normalization():
    """Всё, что персона говорит, должно укладываться в алфавит движка.

    У Vosk в нём 63 символа, и посторонний знак роняет синтез. Проверять
    нужно именно нормализованный текст — до синтеза он проходит через
    `normalize_for_speech`.
    """
    persona = Persona(choice=_first)
    for situation in SITUATIONS:
        for language in ("ru", "en"):
            for line in persona.variants(situation, language):
                text = normalize_for_speech(
                    line.format(address="сэр", greeting="Доброе утро"),
                    language=language,
                )
                assert text.strip(), line
                if language == "ru":
                    assert not any(char.isascii() and char.isalpha() for char in text)


# --- подстановки ----------------------------------------------------------


def test_address_is_substituted():
    """«{address}» превращается в обращение из конфига."""
    persona = Persona(address={"ru": "босс"}, choice=_first)
    line = persona.line(LISTENING, "ru")
    assert "босс" in line
    assert "{" not in line


def test_empty_address_removes_the_comma_too():
    """Без обращения не должно остаться «Слушаю, .»."""
    persona = Persona(address={"ru": ""}, choice=_first)
    for situation in SITUATIONS:
        line = persona.line(situation, "ru")
        assert "{" not in line
        assert ", ." not in line
        assert not line.startswith(",")


def test_single_address_covers_every_language():
    """``address: сэр`` строкой — значит, на любом языке так."""
    persona = Persona(address={"*": "сэр"}, choice=_first)
    assert persona.address_for("ru") == "сэр"
    assert persona.address_for("en") == "сэр"


def test_greeting_depends_on_time_of_day():
    """«Доброе утро» в семь и «Добрый вечер» в девять."""
    assert daypart("ru", now=datetime(2026, 7, 29, 7)) == "Доброе утро"
    assert daypart("ru", now=datetime(2026, 7, 29, 14)) == "Добрый день"
    assert daypart("ru", now=datetime(2026, 7, 29, 21)) == "Добрый вечер"
    assert daypart("ru", now=datetime(2026, 7, 29, 3)) == "Доброй ночи"
    assert daypart("en", now=datetime(2026, 7, 29, 7)) == "Good morning"


def test_greeting_line_contains_time_of_day():
    """В приветствии подставлено время суток, а не «{greeting}»."""
    line = Persona(choice=_first).line(GREETING, "ru")
    assert line.startswith(("Доброе", "Добрый", "Доброй"))


def test_language_falls_back_to_default():
    """Для языка без набора берётся основной — лучше с акцентом, чем молча."""
    persona = Persona(default_language="ru", choice=_first)
    assert persona.line(DONE, "de")


def test_regional_code_is_accepted():
    """``ru-RU`` — тот же русский."""
    # Разные объекты: у одного второй вызов ушёл бы к следующему варианту.
    assert Persona(choice=_first).line(FAILED, "ru-RU") == Persona(
        choice=_first
    ).line(FAILED, "ru")


def test_unknown_situation_returns_nothing():
    """Спросили несуществующее — молчим, а не падаем в середине разговора."""
    assert Persona().line("несуществующая ситуация") == ""


# --- повторы --------------------------------------------------------------


def test_recent_lines_are_not_repeated():
    """Половина набора не повторяется: подряд одна и та же фраза не звучит."""
    persona = Persona(choice=_first)
    pool = persona.variants(LISTENING, "ru")
    said = [persona.line(LISTENING, "ru") for _ in range(len(pool) // 2)]
    assert len(set(said)) == len(said)


def test_pool_is_reused_when_exhausted():
    """Когда свежие варианты кончились, берём любые — но говорить надо."""
    persona = Persona(choice=_first)
    pool = persona.variants(FAREWELL, "ru")
    said = [persona.line(FAREWELL, "ru") for _ in range(len(pool) * 3)]
    assert all(said)


def test_languages_do_not_share_history():
    """Английский набор не должен обедняться из-за русских реплик."""
    persona = Persona(choice=_first)
    persona.line(LISTENING, "ru")
    first = persona.variants(LISTENING, "en")[0].format(address="sir")
    assert persona.line(LISTENING, "en") == first


# --- настройка ------------------------------------------------------------


def test_own_phrases_extend_the_defaults():
    """Свои фразы добавляются к встроенным, а не вытесняют их."""
    persona = Persona(phrases={LISTENING: {"ru": ["Чего изволите?"]}})
    variants = persona.variants(LISTENING, "ru")
    assert "Чего изволите?" in variants
    assert len(variants) > 1


def test_replace_drops_the_defaults():
    """``replace: true`` оставляет только свои — и только там, где они есть."""
    persona = Persona(phrases={LISTENING: {"ru": ["Чего изволите?"]}}, replace=True)
    assert persona.variants(LISTENING, "ru") == ("Чего изволите?",)
    # Английский не трогали — он остался встроенным.
    assert len(persona.variants(LISTENING, "en")) >= 10


def test_unknown_situation_in_config_warns(caplog):
    """Опечатка в названии ситуации иначе осталась бы незамеченной.

    Ровно на этом уже обжигались с `router.aliases`: синоним вёл на
    несуществующий инструмент, и команда молча уезжала в платную модель.
    """
    with caplog.at_level("WARNING"):
        Persona(phrases={"listenng": {"ru": ["Ага"]}})
    assert "listenng" in caplog.text


def test_blank_lines_are_ignored():
    """Пустая строка в конфиге не должна превращаться в паузу вместо ответа."""
    persona = Persona(phrases={DONE: {"ru": ["  ", ""]}}, replace=True)
    assert persona.variants(DONE, "ru")


def test_unknown_placeholder_does_not_break_the_reply(caplog):
    """Своя фраза с чужой подстановкой произносится как есть, с записью в лог."""
    persona = Persona(phrases={DONE: {"ru": ["Готово, {mode}."]}}, replace=True)
    with caplog.at_level("WARNING"):
        line = persona.line(DONE, "ru")
    assert line
    assert "mode" in caplog.text


# --- характер для модели --------------------------------------------------


def test_style_mentions_the_address():
    """Модель должна знать, как обращаться к собеседнику."""
    style = Persona(address={"ru": "сэр"}).style("ru")
    assert "сэр" in style
    assert "{" not in style


def test_style_without_address_says_nothing_about_it():
    """Обращение убрали — модель не должна выдумывать своё."""
    style = Persona(address={"ru": ""}).style("ru")
    assert "Обращайся" not in style


def test_summary_lists_every_situation():
    """Отчёт ``--check`` показывает, что персона действительно собралась."""
    summary = Persona().summary()
    assert "сэр" in summary
    for situation in SITUATIONS:
        assert situation in summary


@pytest.mark.parametrize("situation", SITUATIONS)
def test_random_choice_stays_inside_the_pool(situation):
    """Со случайным выбором реплика всё равно из своего набора."""
    persona = Persona()
    pool = {
        line.format(address="сэр", greeting=daypart("ru"))
        for line in persona.variants(situation, "ru")
    }
    assert persona.line(situation, "ru") in pool
