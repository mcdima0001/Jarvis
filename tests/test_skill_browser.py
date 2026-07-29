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


def test_google_the_verb_is_not_google_the_engine() -> None:
    """«Загугли» — это «поищи», а не требование конкретного поисковика.

    Фраза не называет поисковик, поэтому берётся тот, что стоит в конфиге.
    Владелец пользуется Яндексом, и «загугли борщ» должно открывать его.
    """
    assert browser.pick_engine("", ENGINES, "yandex") == "yandex"


def test_named_engine_beats_the_default() -> None:
    """А вот «найди в гугле» — это уже прямое указание, и оно сильнее."""
    assert browser.pick_engine("гугле", ENGINES, "yandex") == "google"


# --- переключение на уже открытое -------------------------------------------


_WINDOWS = [
    {"title": "YouTube - Яндекс Браузер", "image": "browser.exe", "pid": 2000},
    {"title": "Почта - Яндекс Браузер", "image": "browser.exe", "pid": 2000},
    {"title": "FL Studio 21", "image": "FL64.exe", "pid": 5678},
]


def test_browser_name_is_stripped_from_the_title() -> None:
    """Имя браузера стоит в заголовке каждого окна и мешает сравнению.

    Без этого «зайди на яндекс» находило бы любое окно Яндекс Браузера.
    """
    assert browser.page_title("YouTube - Яндекс Браузер") == "YouTube"
    assert browser.page_title("Почта — Яндекс Браузер") == "Почта"
    assert browser.page_title("Блокнот") == "Блокнот"


def test_site_keys_collect_every_spelling() -> None:
    """Сайт узнаётся по любому своему написанию из каталога."""
    keys = browser.site_keys("https://www.youtube.com", SITES, "ютуб")
    assert {"ютуб", "ютьюб", "youtube"} <= keys


def test_site_keys_ignore_the_domain_of_known_sites() -> None:
    """Домен почты дал бы «google», и она нашлась бы в любом окне с гуглом."""
    keys = browser.site_keys("https://mail.google.com", SITES, "почта")
    assert "google" not in keys


def test_domain_used_when_the_site_has_no_name() -> None:
    """У сайта, названного доменом, других написаний просто нет."""
    assert "example" in browser.site_keys("https://example.com", SITES)


def test_open_window_found_by_title() -> None:
    """Открытая вкладка видна по заголовку окна."""
    keys = browser.site_keys("https://www.youtube.com", SITES, "ютуб")
    assert browser.find_open_window(_WINDOWS, keys) == "YouTube - Яндекс Браузер"


def test_window_of_another_program_is_not_a_tab() -> None:
    """Окно с похожим заголовком, но не браузера, вкладкой не считается."""
    windows = [{"title": "YouTube", "image": "explorer.exe", "pid": 1}]
    keys = browser.site_keys("https://www.youtube.com", SITES, "ютуб")
    assert browser.find_open_window(windows, keys) is None


def test_no_open_window_means_open_a_new_one() -> None:
    """Ничего похожего не открыто — переключаться не на что."""
    keys = browser.site_keys("https://github.com", SITES, "гитхаб")
    assert browser.find_open_window(_WINDOWS, keys) is None


def test_word_inside_another_word_does_not_count() -> None:
    """«Youtube» внутри «myyoutubedownloader» — это не открытый YouTube."""
    windows = [{"title": "myyoutubedownloader - Яндекс Браузер",
                "image": "browser.exe", "pid": 2000}]
    keys = browser.site_keys("https://www.youtube.com", SITES, "ютуб")
    assert browser.find_open_window(windows, keys) is None


# --- мост к расширению ------------------------------------------------------


def test_saved_token_survives_restart(tmp_path) -> None:
    """Токен читается обратно, а не создаётся заново на каждый запуск.

    Новый токен при каждом старте означал бы, что расширение с прочитанным
    в память старым перестаёт подключаться до перезагрузки браузера.
    """
    path = tmp_path / "token.json"
    path.write_text('{"token": "секрет", "port": 8765}', encoding="utf-8")

    assert browser.read_token(path) == "секрет"


@pytest.mark.parametrize("content", ['{"port": 1}', "не json", ""])
def test_broken_token_file_is_ignored(tmp_path, content: str) -> None:
    """Испорченный файл — повод создать новый токен, а не упасть."""
    path = tmp_path / "token.json"
    path.write_text(content, encoding="utf-8")

    assert browser.read_token(path) == ""


def test_missing_token_file_is_ignored(tmp_path) -> None:
    """Первый запуск: файла ещё нет."""
    assert browser.read_token(tmp_path / "нет.json") == ""


class _FakeServer:
    """Сервер, который вместо сети складывает отправленное в список."""

    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.sent: list[dict] = []
        self.answer: dict | None = None
        self.bridge = None

    async def send(self, text: str) -> int:
        import json as _json

        message = _json.loads(text)
        self.sent.append(message)
        if self.answer is not None and self.bridge is not None:
            reply = {"id": message["id"], **self.answer}
            await self.bridge.on_message(_json.dumps(reply))
        return 1


def _bridge(**kwargs):
    """Мост поверх поддельного сервера."""
    import logging

    server = _FakeServer(**kwargs)
    bridge = browser._Extension(
        server=server, logger=logging.getLogger("test-browser"), timeout=0.2
    )
    server.bridge = bridge
    return bridge, server


async def test_reply_is_matched_by_id() -> None:
    """Ответ находит свой запрос по номеру, а не по порядку прихода."""
    bridge, server = _bridge()
    server.answer = {"ok": True, "result": {"tabId": 7, "reused": True}}

    result = await bridge.call("open", url="https://ya.ru", reuse=True)

    assert result == {"tabId": 7, "reused": True}
    assert server.sent[0]["action"] == "open"
    assert server.sent[0]["params"] == {"url": "https://ya.ru", "reuse": True}


async def test_error_from_extension_is_not_a_result() -> None:
    """Расширение доложило об ошибке — значит, делаем по-старому."""
    bridge, server = _bridge()
    server.answer = {"ok": False, "error": "недопустимый адрес"}

    assert await bridge.call("open", url="file:///etc/passwd") is None


async def test_silence_is_not_a_result() -> None:
    """Расширение молчит — ждём недолго и возвращаемся к окнам."""
    bridge, _ = _bridge()

    assert await bridge.call("tabs") is None


async def test_nothing_is_sent_when_disconnected() -> None:
    """Без расширения команда даже не собирается."""
    bridge, server = _bridge(connected=False)

    assert await bridge.call("tabs") is None
    assert server.sent == []


async def test_garbage_from_extension_is_survivable() -> None:
    """Мусор в сокете не должен ронять разбор ответов."""
    bridge, _ = _bridge()

    await bridge.on_message("не json")
    await bridge.on_message('{"event": "hello", "agent": "Chrome"}')
    await bridge.on_message('{"id": 999, "ok": true}')


# --- самое длинное название побеждает ---------------------------------------


@pytest.mark.parametrize(
    "spoken, expected",
    [
        ("Яндекс музыку", "https://music.yandex.ru"),
        ("яндекс музыка", "https://music.yandex.ru"),
        ("музыку", "https://music.yandex.ru"),
        ("яндекс диск", "https://disk.yandex.ru"),
        ("Яндекс", "https://ya.ru"),
    ],
)
def test_longest_site_name_wins(spoken: str, expected: str) -> None:
    """«Яндекс музыку» начинается с «яндекс» — и открывался поиск вместо музыки.

    Какая запись попадётся в словаре первой, такая и выигрывала. Теперь
    побеждает самое длинное подходящее название.
    """
    assert browser.site_url(spoken, SITES) == expected


# --- закрытие вкладки по заголовку ------------------------------------------


_TABS = [
    {"tabId": 1, "title": "Расширения - Яндекс Браузер", "active": False},
    {"tabId": 2, "title": "YouTube", "active": True},
    {"tabId": 3, "title": "Extensions", "active": False},
]


@pytest.mark.parametrize(
    "spoken, expected",
    [("расширения", [1]), ("Extensions", [3]), ("расширени", [1])],
)
def test_tab_found_by_title(spoken: str, expected: list[int]) -> None:
    """Не всякая вкладка — известный сайт.

    Настройки браузера, локальная разработка, открытый документ: «закрой
    вкладку Extensions» упиралось в «не знаю такого сайта».
    """
    assert browser.tabs_by_title(_TABS, spoken) == expected


@pytest.mark.parametrize("spoken", ["", "я", "ог", "  "])
def test_short_words_close_nothing(spoken: str) -> None:
    """По двум буквам закрывать вкладки нельзя — снесёт половину."""
    assert browser.tabs_by_title(_TABS, spoken) == []


def test_unrelated_title_is_not_closed() -> None:
    """Случайное слово не должно совпасть ни с одной вкладкой."""
    assert browser.tabs_by_title(_TABS, "борщ") == []


# --- служебные страницы и переключение на вкладку ----------------------------


@pytest.mark.parametrize(
    "spoken",
    ["расширения", "расширение", "Расширения", "историю", "загрузки", "закладки"],
)
def test_internal_page_recognised(spoken: str) -> None:
    """Служебные страницы браузера открываются только изнутри него.

    `browser://extensions` системе не отдашь: она такую схему не знает.
    Поэтому список закрытый, а адреса перебираются — у Яндекса browser://,
    у Chrome chrome://, у Firefox about:.
    """
    assert browser.internal_page(spoken)


@pytest.mark.parametrize("spoken", ["ютуб", "борщ", "", "и"])
def test_not_an_internal_page(spoken: str) -> None:
    """Обычный сайт служебной страницей не считается."""
    assert browser.internal_page(spoken) == ()


def test_internal_pages_are_not_web_urls() -> None:
    """Ни одна служебная страница не должна пройти как обычная ссылка.

    Иначе она уехала бы в `os.startfile`, а это уже запуск обработчика схемы.
    """
    for urls in browser.INTERNAL_PAGES.values():
        assert all(browser.safe_url(url) is None for url in urls)


def test_quotes_around_the_name_are_stripped() -> None:
    """Whisper ставит ёлочки: «открой вкладку «Marshall Tech»»."""
    assert browser.clean_spoken("«Marshall Tech»") == "Marshall Tech"
    assert browser.site_url("«Ютуб»", SITES) == "https://www.youtube.com"
    tabs = [{"tabId": 9, "title": "Marshall Tech - YouTube"}]
    assert browser.tabs_by_title(tabs, "«Marshall Tech»") == [9]
