"""Короткая память на реплики: ассистент помнит, о чём только что был разговор.

Случай, ради которого это сделано, взят из живого запуска 12.09.2026: ассистент
спросил «а что написать Роме?», владелец ответил — и ответ ушёл в роутер новой
командой ниоткуда, потому что про свой вопрос ассистент уже не помнил.
"""

from __future__ import annotations

from jarvis.core.dialogue import MAX_TEXT, TTL_S, Conversation
from jarvis.core.situation import Situation
from jarvis.core.state import Modes

# --- что помним -------------------------------------------------------------


def test_turns_keep_both_sides_in_order() -> None:
    """Разговор — это обе стороны подряд, а не одни только команды."""
    talk = Conversation()
    talk.said("напиши Роме")
    talk.replied("А что написать?")
    talk.said("что задача готова")

    assert [(turn.role, turn.text) for turn in talk.turns()] == [
        ("user", "напиши Роме"),
        ("assistant", "А что написать?"),
        ("user", "что задача готова"),
    ]


def test_messages_carry_roles_for_the_model() -> None:
    """Модели реплики отдаются ролями: пересказывать их словами — платить зря."""
    talk = Conversation()
    talk.said("как дела")
    talk.replied("Всё работает.")

    assert [(msg.role, msg.content) for msg in talk.messages()] == [
        ("user", "как дела"),
        ("assistant", "Всё работает."),
    ]


def test_only_the_last_turns_survive() -> None:
    """Разговор оплачивается на каждом вопросе, поэтому он короткий."""
    talk = Conversation(turns=4)
    for number in range(10):
        talk.said(f"реплика {number}")

    texts = [turn.text for turn in talk.turns()]
    assert texts == ["реплика 6", "реплика 7", "реплика 8", "реплика 9"]


def test_long_reply_is_cut() -> None:
    """Пересказ переписки в историю целиком не лезет: важно, о чём была речь."""
    talk = Conversation()
    talk.replied("а" * (MAX_TEXT * 3))

    assert len(talk.turns()[0].text) == MAX_TEXT


def test_empty_text_is_not_a_turn() -> None:
    """Пустая реплика — не реплика."""
    talk = Conversation()
    talk.said("   ")
    talk.replied("")

    assert talk.turns() == ()


# --- что забываем -----------------------------------------------------------


def test_old_talk_is_forgotten() -> None:
    """Разговор часовой давности — не контекст, а помеха: модель ответит невпопад."""
    talk = Conversation()
    talk.said("напиши Роме", now=0.0)
    talk.replied("А что написать?", now=1.0)

    assert talk.turns(now=TTL_S + 2) == ()


def test_a_pause_ends_the_talk_not_the_assistant() -> None:
    """После долгой паузы разговор начинается заново, а не продолжается."""
    talk = Conversation()
    talk.said("напиши Роме", now=0.0)
    talk.said("какая погода", now=TTL_S + 1)

    assert [turn.text for turn in talk.turns(now=TTL_S + 1)] == ["какая погода"]


def test_clear_forgets_everything() -> None:
    """«Забудь, о чём говорили» обязано работать без перезапуска."""
    talk = Conversation()
    talk.said("напиши Роме")
    talk.clear()

    assert talk.turns() == ()


def test_disabled_remembers_nothing_at_all() -> None:
    """Выключенный не копит реплики в памяти процесса, а не «копит и молчит»."""
    talk = Conversation(enabled=False)
    talk.said("напиши Роме")
    talk.replied("А что написать?")

    assert not talk.enabled
    assert talk.turns() == ()
    assert talk.messages() == []
    assert talk.describe("ru") == ""


# --- одна строка для разбора ------------------------------------------------


def test_prompt_line_is_the_assistants_own_question() -> None:
    """В подсказку разбора идёт то, что ассистент сказал последним."""
    talk = Conversation()
    talk.said("напиши Роме")
    talk.replied("А что написать Ромка Малютка?")
    talk.said("что задача готова")

    line = talk.describe("ru")
    assert "А что написать Ромка Малютка?" in line
    assert "что задача готова" not in line, "в подсказку уехала и реплика владельца"


def test_prompt_line_speaks_the_asked_language() -> None:
    """Обстановка собирается на языке фразы — строка разговора тоже."""
    talk = Conversation()
    talk.replied("Anything else?")

    assert talk.describe("en").startswith("You just said")


def test_nothing_said_means_no_line() -> None:
    """Пустых разделов в подсказке не бывает: чего нет, о том не упоминаем."""
    talk = Conversation()
    talk.said("какая погода")

    assert talk.describe("ru") == ""


def test_situation_carries_the_last_reply() -> None:
    """Обстановка доносит вопрос ассистента до разбора — иначе всё зря."""
    talk = Conversation()
    talk.replied("А что написать Ромка Малютка?")
    situation = Situation(modes=Modes(), conversation=talk)

    assert "А что написать Ромка Малютка?" in situation.describe("ru")


def test_situation_without_conversation_still_works() -> None:
    """Обстановка собирается и без разговора: он необязателен по построению."""
    assert Situation(modes=Modes()).describe("ru")
