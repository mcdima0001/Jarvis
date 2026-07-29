"""Скилл браузера: разбор сайтов, поисковиков и ссылок.

Открывать окна на сервере некому, поэтому вся логика — чистые функции, и
проверяется она где угодно. Наружу уходит ровно одна вещь — ссылка, и именно
её здесь и разбираем.
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
    path = _ROOT / "skills" / "browser" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_browser", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


browser = _load()
SITES = dict(browser.SITES)
ENGINES = dict(browser.ENGINES)


# --- безопасность ссылок ----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/System32/cmd.exe",
        "steam://uninstall/440",
        "javascript:alert(1)",
        "ms-msdt:/id",
        "https://",
        "просто текст",
    ],
)
def test_dangerous_url_rejected(url: str) -> None:
    """Открывается только http и https.

    Ссылка уходит в систему, а та по схеме выбирает, что запустить. Разрешить
    произвольную схему — значит разрешить голосу запускать обработчик
    протокола, то есть почти что угодно.
    """
    assert browser.safe_url(url) is None


@pytest.mark.parametrize(
    "url",
    ["https://ya.ru", "http://example.com/path?a=1", "https://www.youtube.com"],
)
def test_ordinary_url_allowed(url: str) -> None:
    """Обычные ссылки проходят."""
    assert browser.safe_url(url) == url


# --- сайты ------------------------------------------------------------------


@pytest.mark.parametrize(
    "spoken, expected",
    [
        ("ютуб", "https://www.youtube.com"),
        ("ютубе", "https://www.youtube.com"),
        ("Ютуб", "https://www.youtube.com"),
        ("гитхаб", "https://github.com"),
        ("почту", "https://mail.google.com"),
        ("чат гпт", "https://chatgpt.com"),
    ],
)
def test_known_sites_found(spoken: str, expected: str) -> None:
    """Название узнаётся в любом падеже: сравнение идёт началом слова."""
    assert browser.site_url(spoken, SITES) == expected


def test_bare_domain_gets_https() -> None:
    """«Зайди на example.com» — это сайт, а не название."""
    assert browser.site_url("example.com", SITES) == "https://example.com"
    assert browser.site_url("ya.ru/maps", SITES) == "https://ya.ru/maps"


def test_full_link_kept_as_is() -> None:
    """Готовая ссылка не переписывается."""
    assert browser.site_url("https://ya.ru", SITES) == "https://ya.ru"


@pytest.mark.parametrize("spoken", ["", "я", "как дела", "открой", "мама"])
def test_not_a_site(spoken: str) -> None:
    """Случайные слова сайтом не считаются.

    Особенно короткие: «я» иначе совпало бы началом с «яндекс», и любой
    вопрос со словом «я» открывал бы поисковик.
    """
    assert browser.site_url(spoken, SITES) is None


def test_dangerous_site_in_config_ignored() -> None:
    """Проверка схемы работает и для сайтов из конфига."""
    assert browser.site_url("свой", {"свой": "file:///etc/passwd"}) is None


# --- поисковики -------------------------------------------------------------


@pytest.mark.parametrize(
    "spoken, expected",
    [
        ("гугле", "google"),
        ("гугл", "google"),
        ("google", "google"),
        ("яндексе", "yandex"),
        ("ютубе", "youtube"),
        ("википедии", "wikipedia"),
        ("гитхабе", "github"),
        ("картах", "maps"),
    ],
)
def test_engine_recognised_in_any_case(spoken: str, expected: str) -> None:
    """Поисковик приходит из речи в предложном падеже: «в гугле», «на ютубе»."""
    assert browser.pick_engine(spoken, ENGINES, "google") == expected


@pytest.mark.parametrize("spoken", ["", "интернете", "сети", "браузере", "холодильнике"])
def test_unknown_engine_falls_back(spoken: str) -> None:
    """«Найди в интернете» — это «где обычно», а не ошибка."""
    assert browser.pick_engine(spoken, ENGINES, "yandex") == "yandex"


def test_own_engine_from_config() -> None:
    """Свой поисковик из конфига участвует наравне со встроенными."""
    engines = {**ENGINES, "рутрекер": "https://rutracker.org/?nm={query}"}
    assert browser.pick_engine("рутрекер", engines, "google") == "рутрекер"


# --- поисковая ссылка -------------------------------------------------------


def test_query_is_encoded() -> None:
    """Запрос кодируется целиком — иначе он выходит за пределы параметра."""
    url = browser.search_url("смешные котики", ENGINES["google"])
    assert url == "https://www.google.com/search?q=%D1%81%D0%BC%D0%B5%D1%88%D0%BD%D1%8B%D0%B5+%D0%BA%D0%BE%D1%82%D0%B8%D0%BA%D0%B8"


def test_special_characters_cannot_break_out() -> None:
    """Кавычки, амперсанд и слэш остаются частью запроса, а не ссылки."""
    url = browser.search_url('a&b="c"/d', ENGINES["duckduckgo"])
    assert url is not None
    assert url.startswith("https://duckduckgo.com/?q=")
    assert "&b" not in url and '"' not in url


def test_empty_query_is_refused() -> None:
    """Пустой запрос открывать нечем."""
    assert browser.search_url("   ", ENGINES["google"]) is None


# --- браузеры ---------------------------------------------------------------


@pytest.mark.parametrize(
    "spoken, image",
    [
        ("хром", "chrome.exe"),
        ("гугл хром", "chrome.exe"),
        ("фаерфокс", "firefox.exe"),
        ("мозила", "firefox.exe"),
        ("эдж", "msedge.exe"),
        ("яндекс браузер", "browser.exe"),
    ],
)
def test_browser_named_aloud(spoken: str, image: str) -> None:
    """Браузер узнаётся по тому, как его называют вслух."""
    assert browser.browser_process(spoken) == image


def test_unknown_browser() -> None:
    """Незнакомое слово браузером не считается."""
    assert browser.browser_process("нетскейп") is None


def test_running_browser_found_among_processes() -> None:
    """Из списка процессов выбирается тот, который браузер."""
    processes = ["explorer.exe", "steam.exe", "firefox.exe", "svchost.exe"]
    assert browser.running_browser(processes) == "firefox.exe"


def test_no_browser_among_processes() -> None:
    """Если браузера нет, закрывать нечего."""
    assert browser.running_browser(["explorer.exe", "steam.exe"]) is None


# --- ослышки Whisper --------------------------------------------------------


@pytest.mark.parametrize(
    "misheard, expected",
    [
        ("гитхап", "https://github.com"),
        ("твитч", "https://www.twitch.tv"),
        ("ютьюб", "https://www.youtube.com"),
    ],
)
def test_misheard_name_still_found(misheard: str, expected: str) -> None:
    """Whisper глушит звонкие на конце: «гитхаб» приходит как «гитхап».

    Порог похожести высокий: сайт открывается молча, и промахнуться тут
    неприятнее, чем переспросить.
    """
    assert browser.site_url(misheard, SITES) == expected


@pytest.mark.parametrize("spoken", ["YouTube", "GitHub", "Telegram", "Steam"])
def test_latin_spelling_recognised(spoken: str) -> None:
    """Английские названия Whisper пишет латиницей — как их и произносят.

    «Зайди на YouTube» приходило именно так и не находилось: в каталоге были
    только русские написания.
    """
    assert browser.site_url(spoken, SITES) is not None


@pytest.mark.parametrize("spoken", ["борщ", "анталия", "мама", "как дела", "питон"])
def test_ordinary_words_are_not_sites(spoken: str) -> None:
    """Нечёткое сравнение не должно превращать любое слово в сайт."""
    assert browser.site_url(spoken, SITES) is None
