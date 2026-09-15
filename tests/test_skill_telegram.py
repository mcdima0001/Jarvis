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


# --- картинка из буфера -----------------------------------------------------


@pytest.mark.parametrize(("heard", "saved"), [
    ("Избранное", True), ("выбранное", True), ("сохранённые", True), ("мама", False),
])
def test_saved_messages_are_recognised(heard: str, saved: bool) -> None:
    assert telegram.is_saved_messages(heard) is saved


@pytest.mark.parametrize(("heard", "chat"), [
    ("выбранное в телеграме", "выбранное"),  # живой запуск 14.09.2026
    ("в Избранное", "Избранное"),
    ("маме, в telegram", "маме"),
])
def test_chat_is_cleaned_of_prepositions(heard: str, chat: str) -> None:
    assert telegram.clean_chat(heard) == chat


class _FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[Any, bytes, str, Any]] = []

    async def get_dialogs(self, limit: int = 0) -> list[Any]:
        from types import SimpleNamespace

        return [SimpleNamespace(name="Мама ❤️", entity="mama-entity")]

    async def send_file(self, entity: Any, file: Any, caption: Any = None) -> None:
        self.sent.append((entity, file.getvalue(), file.name, caption))


class _FakeTools:
    def __init__(self, picture: Any) -> None:
        self.picture = picture

    async def invoke(self, name: str, arguments: Any = None) -> Any:
        from jarvis.core.contracts import ToolResult

        assert name == "clipboard.image"
        if self.picture is None:
            return ToolResult.failure("в буфере обмена нет картинки")
        return ToolResult.success(self.picture)


def _telegram(picture: Any) -> Any:
    import logging
    from types import SimpleNamespace

    skill = telegram.TelegramSkill()
    skill._context = SimpleNamespace(tools=_FakeTools(picture), logger=logging.getLogger("test.telegram"))
    skill._client = _FakeClient()
    skill._api_id, skill._api_hash, skill._names = 1, "hash", []
    return skill


async def test_image_goes_to_saved_messages() -> None:
    skill = _telegram({"png": b"\x89PNG", "width": 4, "height": 3})
    result = await skill.send_image("выбранное в телеграме")
    assert result.ok
    assert skill._client.sent == [("me", b"\x89PNG", "screenshot.png", None)]
    assert result.speech_for("ru") == "Отправил картинку в Избранное."


async def test_image_goes_to_a_named_chat() -> None:
    skill = _telegram({"png": b"\x89PNG", "width": 4, "height": 3})
    result = await skill.send_image("маме", caption="смотри")
    assert result.ok and skill._client.sent[0][0] == "mama-entity"
    assert skill._client.sent[0][3] == "смотри"


async def test_nothing_is_sent_without_an_image() -> None:
    skill = _telegram(None)
    result = await skill.send_image("Избранное")
    assert not result.ok and skill._client.sent == []
    assert "нет картинки" in result.speech_for("ru")


async def test_unknown_chat_sends_nothing() -> None:
    skill = _telegram({"png": b"\x89PNG", "width": 4, "height": 3})
    result = await skill.send_image("кутузов")
    assert not result.ok and skill._client.sent == []


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


# --- как сообщают о входящем ------------------------------------------------


def test_incoming_is_announced_with_a_preview() -> None:
    """Вслух идёт кто написал и начало сообщения.

    Целиком зачитывать нельзя: это уведомление о том, что написали, а не чтение
    переписки — для чтения есть отдельная команда.
    """
    said = telegram.announcement("Мама", "буду дома к семи, купи хлеба")
    assert said == "Мама пишет: буду дома к семи, купи хлеба"


def test_long_message_is_cut() -> None:
    """Длинное режется и помечается многоточием."""
    said = telegram.announcement("Чат", "а" * 500)
    assert len(said) < 120
    assert said.endswith("…")


def test_message_without_text_is_still_announced() -> None:
    """Картинка или стикер — тоже повод сказать, что написали."""
    assert telegram.announcement("Вася", "") == "Вася что-то прислал в телеграме."
    assert telegram.announcement("Вася", "   ").endswith("прислал в телеграме.")


def test_line_breaks_do_not_leak_into_speech() -> None:
    """Перевод строки в реплике синтезу не нужен и звучит паузой не там."""
    assert "\n" not in telegram.announcement("Чат", "первая\nвторая")


# --- поручение, а не сообщение ----------------------------------------------


@pytest.mark.parametrize(
    ("dictated", "verb"),
    [("спроси как дела и как настроение", "спроси"), ("и скажи, что буду позже", "скажи"),
     ("буду через час", None), ("привет, как дела", None)],
)
def test_indirect_request_is_recognised(dictated: str, verb: str | None) -> None:
    assert telegram.indirect_verb(dictated) == verb


@pytest.mark.parametrize(
    ("dictated", "message"),
    [("спроси как дела и как настроение", "Как дела и как настроение?"),
     ("скажи, что буду позже", "Буду позже."),
     ("узнай у него, где ключи", "Где ключи?"),
     ("поздравь её с днём рождения", None)],
)
def test_plain_rewrite_without_model(dictated: str, message: str | None) -> None:
    assert telegram.plain_rewrite(dictated) == message


class _SendingClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[tuple[Any, str]] = []

    async def send_message(self, entity: Any, text: str) -> None:
        self.messages.append((entity, text))


class _FakeLLM:
    available = True

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def ask(self, prompt: str, *, task: str | None = None) -> str:
        self.asked.append(prompt)
        return self.answer


def _messenger(llm: Any = None) -> Any:
    skill = _telegram(None)
    skill._client = _SendingClient()
    skill._context.llm = llm
    return skill


async def test_instruction_becomes_a_message_from_the_owner() -> None:
    """15.09.2026, 10:35: Роме ушло буквально «спроси как дела и как настроение»."""
    skill = _messenger(_FakeLLM("«Как дела? Как настроение?»"))
    result = await skill.send_message("маме", text="спроси как дела и как настроение")
    assert result.ok
    assert skill._client.messages == [("mama-entity", "Как дела? Как настроение?")]
    assert result.speech_for("ru") == "Отправил Мама ❤️: Как дела? Как настроение?"


async def test_without_model_the_simple_rule_rewrites() -> None:
    skill = _messenger()
    await skill.send_message("маме", text="спроси как дела и как настроение")
    assert skill._client.messages == [("mama-entity", "Как дела и как настроение?")]


async def test_instruction_that_cannot_be_rewritten_is_not_sent() -> None:
    skill = _messenger()
    result = await skill.send_message("маме", text="поздравь её с днём рождения")
    assert not result.ok and skill._client.messages == []
    assert "дословно" in result.speech_for("ru")


async def test_ordinary_text_goes_as_dictated() -> None:
    llm = _FakeLLM("не должно понадобиться")
    skill = _messenger(llm)
    result = await skill.send_message("маме", text="буду через час")
    assert skill._client.messages == [("mama-entity", "буду через час")]
    assert llm.asked == [] and result.speech_for("ru") == "Отправил Мама ❤️."
