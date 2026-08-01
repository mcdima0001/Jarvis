"""Поиск и погода: разбор ответов источников без похода в сеть.

Сетевые вызовы здесь не проверяются намеренно — они зависят от чужих сервисов
и в тестах ничего не доказывают. Проверяется то, что ломалось: разбор выдачи и
приведение города к именительному падежу.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str) -> Any:
    """Загрузить скилл как модуль: они лежат вне пакета, это плагины."""
    path = _ROOT / "skills" / name / "skill.py"
    spec = importlib.util.spec_from_file_location(f"skill_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Регистрация обязательна: dataclass ищет модуль класса в sys.modules,
    # и без неё падает с AttributeError. Настоящий загрузчик скиллов делает
    # то же самое.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


search = _load("search")
weather = _load("weather")


# --- поиск ------------------------------------------------------------------


def test_duckduckgo_markup_parsed() -> None:
    """Из выдачи достаются заголовок, ссылка и описание."""
    page = (
        '<tr><td><a class="result-link" href="https://ru.wikipedia.org/wiki/Тверь">'
        "Тверь — <b>Википедия</b></a></td></tr>"
        '<tr><td class="result-snippet">Город в России, центр области.</td></tr>'
    )
    results = search._DDG_LINK.findall(page)
    snippets = search._DDG_SNIPPET.findall(page)

    assert search._plain(results[0][1]) == "Тверь — Википедия"
    assert results[0][0] == "https://ru.wikipedia.org/wiki/Тверь"
    assert search._plain(snippets[0]) == "Город в России, центр области."


def test_html_entities_restored() -> None:
    """HTML-мнемоники и лишние пробелы до речи доходить не должны."""
    assert search._plain("Кофе &amp; сигареты\n  <b>тут</b>") == "Кофе & сигареты тут"


def test_providers_registered() -> None:
    """Оба источника доступны по имени из конфига."""
    assert set(search.PROVIDERS) == {"wikipedia", "duckduckgo"}
    assert search.PROVIDERS["wikipedia"]().name == "wikipedia"


# --- погода -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("Москве", "Москва"),
        ("Праге", "Прага"),
        ("Лондоне", "Лондон"),
        ("Твери", "Тверь"),
        ("Афинах", "Афины"),
        ("Нижнем Новгороде", "Нижний Новгород"),
        ("Ростове-на-Дону", "Ростов-на-Дону"),
    ],
)
def test_city_restored_to_nominative(spoken: str, expected: str) -> None:
    """Город приходит из команды в предложном падеже.

    «Погода в Праге» — геокодер знает только «Прага», поэтому написания
    перебираются, и правильное обязано оказаться среди кандидатов.
    """
    assert expected in weather._nominative_candidates(spoken)


def test_nominative_city_comes_first() -> None:
    """Если город уже в именительном, первым пробуется он сам."""
    assert weather._nominative_candidates("Прага")[0] == "Прага"
    assert weather._nominative_candidates("Сочи")[0] == "Сочи"


def test_colloquial_names_resolved() -> None:
    """Разговорные названия в справочнике геокодера отсутствуют."""
    assert weather._nominative_candidates("Питере") == ["Санкт-Петербург"]
    assert weather._nominative_candidates("спб") == ["Санкт-Петербург"]


def test_candidate_count_is_bounded() -> None:
    """Каждый кандидат — запрос к геокодеру, поэтому их число ограничено."""
    candidates = weather._nominative_candidates("Нижнем Новгороде")
    assert len(candidates) <= weather._MAX_CANDIDATES


def test_weather_codes_translated() -> None:
    """Код WMO превращается в описание на языке ответа."""
    assert weather._describe(95, "ru") == "гроза"
    assert weather._describe(95, "en") == "thunderstorm"
    # Неизвестный код не должен ронять ответ.
    assert weather._describe(12345, "ru")


def test_day_words_understood() -> None:
    """«Завтра» и «tomorrow» означают один и тот же день."""
    assert weather._DAY_WORDS["завтра"] == weather._DAY_WORDS["tomorrow"] == 1
    assert weather._DAY_WORDS["сегодня"] == 0


# --- дата в подсказке модели ------------------------------------------------


def test_dialog_prompt_carries_current_date() -> None:
    """Модель не знает, какой сегодня день, — дату нужно передать явно.

    Без этого на «какой сегодня день» она отвечает выдуманной датой.
    """
    from datetime import datetime

    from jarvis.core.situation import now_line as _now_line

    today = datetime.now().astimezone()
    line = _now_line("ru")
    assert str(today.year) in line
    assert f"{today:%H:%M}" in line
    assert str(today.year) in _now_line("en")


# --- источники перебираются по ответу, а не по наличию страниц ---------------


def test_no_answer_marker_is_recognized() -> None:
    """Метка «ответа нет» отличается от ответа, начинающегося с «нет».

    Обычное «нет» бывает и настоящим ответом («правда ли, что…» — «Нет, это
    миф»), и по началу строки такой ответ мы бы выбросили.
    """
    assert search._unanswered("НЕТ ОТВЕТА")
    assert search._unanswered("  нет ответа.  ")
    assert search._unanswered("NO ANSWER")
    assert not search._unanswered("Нет, это миф. Борщ варят из свёклы.")
    assert not search._unanswered("Борщ варят из свёклы и капусты.")


async def test_source_with_pages_but_no_answer_is_not_the_last_word() -> None:
    """«Как варить борщ» Википедия находит статьёй о самом супе.

    Живой случай 01.08.2026: страницы были, ответа не было, перебор на этом
    заканчивался — и вслух звучало «в выдержках нет информации о том, как
    варить борщ», хотя DuckDuckGo с рецептом даже не спросили.
    """
    from types import SimpleNamespace

    asked: list[str] = []

    class Source:
        def __init__(self, name: str, snippet: str) -> None:
            self.name = name
            self._snippet = snippet

        async def search(self, client, query, limit, language):
            asked.append(self.name)
            return [{"title": self.name, "snippet": self._snippet, "url": ""}]

    replies = iter(["НЕТ ОТВЕТА", "Свёклу пассеруют, потом варят с капустой."])

    class LLM:
        available = True

        async def ask(self, prompt, *, task=None, system=None, context=None):
            return next(replies)

    skill = search.SearchSkill()
    skill._max_results = 3
    skill._client = SimpleNamespace()
    skill._providers = [Source("wikipedia", "Борщ — суп."), Source("duckduckgo", "Рецепт.")]
    skill._context = SimpleNamespace(llm=LLM(), logger=logging.getLogger("test.search"))

    result = await skill.answer("как варить борщ")

    assert asked == ["wikipedia", "duckduckgo"], "второй источник обязан быть спрошен"
    assert "Свёклу" in result.speech_for("ru")


async def test_all_sources_silent_is_said_honestly() -> None:
    """Никто не ответил — так и говорим, а не зачитываем отказ модели."""
    from types import SimpleNamespace

    class Source:
        name = "wikipedia"

        async def search(self, client, query, limit, language):
            return [{"title": "Борщ", "snippet": "Борщ — суп.", "url": ""}]

    class LLM:
        available = True

        async def ask(self, prompt, *, task=None, system=None, context=None):
            return "НЕТ ОТВЕТА"

    skill = search.SearchSkill()
    skill._max_results = 3
    skill._client = SimpleNamespace()
    skill._providers = [Source()]
    skill._context = SimpleNamespace(llm=LLM(), logger=logging.getLogger("test.search"))

    result = await skill.answer("как варить борщ")

    assert result.value == ""
    assert "Не нашёл ответа" in result.speech_for("ru")
