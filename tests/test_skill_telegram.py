"""Telegram: разбор адресата и границы, за которые нельзя выходить.

Сети тут нет и не будет: Telethon на сервере не установлен, а проверять надо не
его, а то, что ломается по-настоящему, — кому именно уйдёт сообщение.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load() -> Any:
    """Загрузить скилл как модуль: он плагин и лежит вне пакета."""
    path = _ROOT / "skills" / "telegram" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_telegram", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


telegram = _load()

CHATS = ["Мама ❤️", "Максим", "Работа", "Sasha", "Избранное", "Настя Ко"]


def test_skill_loads_without_telethon() -> None:
    """Скилл грузится, даже когда Telethon не установлен.

    Иначе один необязательный пакет ломает и `--check`, и весь запуск: импорт
    случился бы при загрузке модуля, до всякой проверки настроек.
    """
    assert "telethon" not in sys.modules


# --- кому уйдёт сообщение ---------------------------------------------------


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("мама", "Мама ❤️"),          # эмодзи в названии ничего не значат
        ("Мама", "Мама ❤️"),
        ("максим", "Максим"),
        ("саша", "Sasha"),            # согласный костяк: алфавит выбирает Whisper
        ("работа", "Работа"),
        ("настя", "Настя Ко"),        # начало имени
    ],
)
def test_chat_is_found_by_spoken_name(spoken: str, expected: str) -> None:
    """Услышанное имя сопоставляется с настоящим названием чата."""
    assert telegram.match_chat(spoken, CHATS) == expected


@pytest.mark.parametrize("spoken", ["кутузов", "", "  ", "ма", "вертолёт"])
def test_unknown_chat_is_refused(spoken: str) -> None:
    """Не уверен — не отправляем.

    Имя идёт из речи через Whisper и модель, а цена ошибки тут не «команда не
    сработала», а письмо, прочитанное чужим человеком.
    """
    assert telegram.match_chat(spoken, CHATS) is None


def test_shortest_name_wins() -> None:
    """Из подходящих побеждает самое короткое: «мама» — это «Мама», а не «Мама Юли»."""
    assert telegram.match_chat("мама", ["Мама Юли", "Мама"]) == "Мама"


# --- «напиши маме буду через час» -------------------------------------------


@pytest.mark.parametrize(
    ("spoken", "chat", "text"),
    [
        ("маме буду через час", "Мама ❤️", "буду через час"),
        ("максиму привет", "Максим", "привет"),
        ("настя ко я опоздаю", "Настя Ко", "я опоздаю"),
        ("саша перезвони", "Sasha", "перезвони"),
    ],
)
def test_request_splits_into_who_and_what(spoken: str, chat: str, text: str) -> None:
    """Голосом не диктуют двоеточий: имя и текст приходят одной строкой.

    Делим по самому длинному известному имени в начале фразы — чем длиннее
    совпало, тем меньше шанс, что это случайное слово.
    """
    assert telegram.split_request(spoken, CHATS) == (chat, text)


def test_unknown_addressee_leaves_the_phrase_alone() -> None:
    """Адресата не узнали — текст не портим, дальше будет честный отказ."""
    assert telegram.split_request("кутузову пора домой", CHATS) == ("", "кутузову пора домой")


# --- что произносится вслух -------------------------------------------------


def test_unread_is_spoken_like_a_human() -> None:
    """Реплика — живая фраза, а не выгрузка структуры."""
    said = telegram.describe_dialogs(
        [{"name": "Мама", "unread": 1}, {"name": "Работа", "unread": 4}]
    )

    assert said == "Непрочитано в 2: Мама, Работа — 4."
    assert telegram.describe_dialogs([]) == "Новых сообщений нет."
