"""Скилл страницы: сборка плана, проверка шагов и обучение с записью в память.

Браузера тут нет и быть не может, поэтому проверяется то, что от него не
зависит: во что превращается команда, что из этого разрешено отправлять в
страницу и сколько раз ради одного и того же спрашивается модель.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from jarvis.core.bus import LocalEventBus
from jarvis.core.config import MemoryConfig, SkillsConfig, TaskProfile
from jarvis.core.llm import LLMService, ProfileRegistry
from jarvis.core.llm.protocol import LLMRequest, LLMResponse
from jarvis.core.memory import build_memory
from jarvis.core.skills import SkillManager
from jarvis.core.tools import ToolRegistry
from jarvis.core.tts import NullTTS

_ROOT = Path(__file__).resolve().parent.parent


def _load() -> Any:
    """Загрузить скилл как модуль: он плагин и лежит вне пакета."""
    path = _ROOT / "skills" / "browser" / "page" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_page", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


page = _load()


# --- домен и рецепты --------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://music.yandex.ru/home", "music.yandex.ru"),
        ("https://www.youtube.com/watch?v=1", "youtube.com"),
        ("https://m.youtube.com:443/watch", "m.youtube.com"),
        ("не ссылка", ""),
    ],
)
def test_host_of(url: str, expected: str) -> None:
    """Домен берётся без ``www`` и без порта — по нему ищется рецепт."""
    assert page.host_of(url) == expected


def test_recipe_found_by_subdomain() -> None:
    """Рецепт сайта работает и на его поддоменах: разметка там та же."""
    assert page.recipes_for("m.youtube.com", page.SITE_RECIPES)
    assert page.recipes_for("music.youtube.com", page.SITE_RECIPES)
    assert page.recipes_for("youtube.com.evil.ru", page.SITE_RECIPES) == {}
    assert page.recipes_for("", page.SITE_RECIPES) == {}


# --- что разрешено отправлять в страницу ------------------------------------


@pytest.mark.parametrize(
    "step",
    [
        {"eval": "alert(1)"},
        {"media": "shutdown"},
        {"script": ["fetch('/')"]},
        {"click": []},
        {"label": [""]},
        "не шаг",
        {},
    ],
)
def test_unknown_step_dropped(step: Any) -> None:
    """Набор шагов закрытый: чего в нём нет, того странице не отправить.

    Планы приходят из конфига, из памяти и от языковой модели — верить нельзя
    ни одному из трёх источников. То же правило, что и с названиями программ:
    выполняется только узнанное.
    """
    assert page.validate_plan([step]) == []


def test_valid_steps_survive() -> None:
    """Понятные шаги проходят как есть, повторы схлопываются."""
    plan = page.validate_plan(
        [
            {"media": "PAUSE"},
            {"click": [".ytp-next-button", ".other"]},
            {"label": ["следующий трек"]},
            {"media": "pause"},
        ]
    )
    assert plan == [
        {"media": "pause"},
        {"click": [".ytp-next-button", ".other"]},
        {"label": ["следующий трек"]},
    ]


def test_plan_is_capped() -> None:
    """Длина плана ограничена: перебирать бесконечно нечего."""
    plan = page.validate_plan([{"label": [f"кнопка {number}"]} for number in range(50)])
    assert len(plan) == page.MAX_STEPS


def test_long_selector_dropped() -> None:
    """Строки в шаге ограничены по длине — это данные, а не текст."""
    assert page.validate_plan([{"click": ["a" * (page.MAX_TEXT + 1)]}]) == []


def test_forbidden_labels_survive_validation() -> None:
    """Запретные подписи — часть шага, а не украшение: они его и уточняют."""
    plan = page.validate_plan([{"label": ["нравится"], "avoid": ["не нравится"]}])
    assert plan == [{"label": ["нравится"], "avoid": ["не нравится"]}]
    # У селектора запрещать нечего: он и так указывает на конкретный элемент.
    assert page.validate_plan([{"click": ["#like"], "avoid": ["не"]}]) == [{"click": ["#like"]}]


def test_like_never_means_dislike() -> None:
    """У лайка запрет отрицания обязателен.

    По-русски отрицание стоит перед словом, поэтому «не нравится» содержит
    «нравится» целиком — и совпадением по слову лайк от дизлайка не отличить.
    На живой Яндекс Музыке это стоило дизлайка вместо лайка.
    """
    avoid = page.ACTIONS["like"][0]["avoid"]
    assert "не нравится" in avoid and "dislike" in avoid
    # «Убрать отметку» — тоже кнопка со словом «нравится», и это отмена лайка.
    assert "убрать" in avoid


def test_top_result_is_added_only_for_our_own_search() -> None:
    """«Сначала верхний результат» дописывается к шагу `item`, и только к нему."""
    plan = page.validate_plan(
        [{"item": ["трек"], "prefer": ['[class*="Play"]', "a" * (page.MAX_TEXT + 1)]}]
    )

    assert plan == [{"item": ["трек"], "prefer": ['[class*="Play"]']}]
    # Слишком длинный селектор выбрасывается, как и везде: это данные из
    # конфига и памяти, а не код.
    assert page.validate_plan([{"item": ["трек"], "prefer": ["   "]}]) == [
        {"item": ["трек"]}
    ]


def test_item_step_carries_the_play_hint() -> None:
    """У шага «включить названное» есть подсказка и признак «дожать плеер».

    Нажатие по строке трека воспроизведение не запускает — оно её выбирает.
    Играть начинает кнопка внутри строки, а её имя надо знать.
    """
    plan = page.validate_plan(
        [{"item": ["midnight city"], "hint": ["воспроизв", "play"], "play": True}]
    )

    assert plan == [
        {"item": ["midnight city"], "hint": ["воспроизв", "play"], "play": True}
    ]
    # Без подсказки шаг остаётся рабочим: нажмёт строку и дожмёт плеер.
    assert page.validate_plan([{"item": ["трек"]}]) == [{"item": ["трек"]}]


async def test_named_track_is_played_not_just_clicked(loaded) -> None:
    """«Включи Midnight City» — отдельная команда, а не «нажми».

    Живой случай: «нажми Midnight City» строку нажимало, но музыка не играла, а
    «включи Midnight City» уходило в модель и та включала воспроизведение… на
    ютубе, в соседней вкладке.
    """
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.REPLIES[:] = [{"done": "item", "detail": "Трек Midnight City — Воспроизвести"}]

    result = await registry.invoke("page.play_item", {"track": "Midnight City"})

    assert result.ok
    step = fake.CALLS[1][1][0]
    assert step["item"][0] == "midnight city"
    assert step["play"] is True and step["hint"], "нужна и подсказка, и дожим плеера"
    # Смотрят в одну вкладку, а играть может другая — берём ту, куда смотрят.
    assert fake.CALLS[0] == ("target", "", True)
    await manager.stop()


@pytest.mark.parametrize(
    ("host", "query", "expected"),
    [
        ("music.yandex.ru", "Don't Stop Me Now", "https://music.yandex.ru/search?text=don%27t+stop+me+now"),
        ("m.youtube.com", "мем", "https://www.youtube.com/results?search_query=%D0%BC%D0%B5%D0%BC"),
        ("example.com", "что угодно", ""),
        ("music.yandex.ru", "   ", ""),
    ],
)
def test_site_search_address(host: str, query: str, expected: str) -> None:
    """Свой поиск есть не у всех сайтов, и запрос в адрес попадает закодированным."""
    assert page.search_url_for(host, query.lower()) == expected


async def test_missing_track_is_searched_on_the_site(loaded) -> None:
    """«Включи Don't Stop Me Now» — это «найди и включи».

    Живой случай: трека нет на открытой странице, потому что его туда никто не
    выводил, и команда честно отказывала. Теперь открывается поиск **самого
    сайта** — в той же вкладке, ссылкой, — и попытка повторяется.
    """
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.REPLIES[:] = [
        {"done": None},
        {"done": "item", "detail": "Don't Stop Me Now — Воспроизвести"},
    ]

    result = await registry.invoke("page.play_item", {"track": "Don't Stop Me Now"})

    assert result.ok
    assert [call[0] for call in fake.CALLS] == ["target", "run", "go", "run"]
    assert fake.CALLS[2][1] == "https://music.yandex.ru/search?text=don%27t+stop+me+now"
    # Вкладка та же самая: новая была бы лишней.
    assert fake.CALLS[2][2] == fake.TARGET["tabId"]
    # На своей выдаче к тому же поиску добавляется «сначала верхний результат»:
    # порядок расставил сайт, и знает он больше, чем мы об услышанном названии.
    before, after = fake.CALLS[1][1][0], fake.CALLS[3][1][0]
    assert after["item"] == before["item"]
    assert "prefer" not in before, "на чужой странице верхнего результата нет"
    assert after["prefer"] == ['[class*="PlayButtonWithCover_playButton"]']
    await manager.stop()


async def test_play_by_name_belongs_to_the_open_site(loaded) -> None:
    """«Включи X» решает открытый сайт, и выбор тут не за моделью.

    Двух инструментов на одну просьбу быть не должно: пока в каталоге лежал и
    «включи ролик», модель выбирала наугад и уводила с открытой Яндекс Музыки на
    ютуб. Фразы, где про видео сказано прямо, ведут в свой инструмент — но
    модели он не показан.
    """
    manager, registry, _ = loaded
    await manager.start()
    catalog = {spec.name for spec in registry.specs() if spec.routable}

    assert "page.play_item" in catalog
    assert "page.play_video" not in catalog
    assert registry.has("page.play_video"), "по фразам он доступен"
    await manager.stop()


async def test_track_goes_to_the_music_site_not_youtube(loaded, monkeypatch) -> None:
    """«Включи трек X» — про музыку, даже если открыт посторонний сайт.

    Живой случай: с открытым другим сайтом Jarvis включил клип на ютубе, хотя
    слово «трек» сказано прямо, а Яндекс Музыка была открыта рядом. Где искать
    музыку, ассистент знает и без подсказки — это `music_site` из конфига.
    """
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    monkeypatch.setattr(sys.modules["jarvis_skills.page"], "SEARCH_DELAY", 0)
    monkeypatch.setattr(
        fake, "TARGET", {"tabId": 7, "url": "https://example.com/", "title": "Ничего"}
    )
    monkeypatch.setitem(
        fake.SITE_TARGETS,
        "яндекс музыка",
        {"tabId": 9, "url": "https://music.yandex.ru/home", "title": "Музыка"},
    )
    fake.CALLS.clear()
    fake.REPLIES[:] = [
        {"done": None},  # на посторонней странице трека нет
        {"done": None},  # на главной музыкального сайта тоже
        {"done": "item", "detail": "midnight city", "played": True},  # после поиска
    ]

    result = await registry.invoke("page.play_item", {"track": "Midnight City"})

    assert result.ok
    kinds = [call[0] for call in fake.CALLS]
    assert ("open", "яндекс музыка") in fake.CALLS, "музыкальный сайт нужно открыть"
    assert not any(call[0] == "search" for call in fake.CALLS), "ютуб тут не при чём"
    went = fake.CALLS[kinds.index("go")][1]
    assert went.startswith("https://music.yandex.ru/search")
    await manager.stop()


async def test_clicked_but_silent_is_not_a_success(loaded, monkeypatch) -> None:
    """«Нашёл» и «включил» — разные вещи, и вслух это должно звучать честно.

    Живой случай на Яндекс Музыке: строка нашлась, кнопка «Воспроизведение»
    нажалась, ассистент сказал «включаю» — и тишина.
    """
    manager, registry, _ = loaded
    await manager.start()
    # Ждать по-настоящему тут нечего: страницы нет, а пауза между попытками
    # нужна живому браузеру. Скилл поднят загрузчиком, поэтому и правим его
    # собственный модуль, а не тот, что импортирован тестом.
    monkeypatch.setattr(sys.modules["jarvis_skills.page"], "SEARCH_DELAY", 0)
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    quiet = {"done": "item", "detail": "midnight city — воспроизведение", "played": False}
    fake.REPLIES[:] = [dict(quiet) for _ in range(page.SEARCH_ATTEMPTS + 1)]

    result = await registry.invoke("page.play_item", {"track": "Midnight City"})

    assert not result.ok, "тишина — это не успех"
    assert "не началось" in (result.error or "")
    # Вслух называется то, что просили. Подпись найденного бывает какой угодно:
    # однажды в неё уехала склеенная выдача целиком, и ассистент прочитал её.
    assert result.speech_for("ru") == "Нашёл midnight city, но включить не получилось."
    # Круг ровно один: не нашли на странице — поискали на сайте. Дальше
    # повторять нечего, строка найдена, и все способы включить её страница уже
    # перебрала. Второй круг — это столько же нажатий впустую.
    assert [call[0] for call in fake.CALLS] == ["target", "run", "go", "run"]
    await manager.stop()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=abc", "https://www.youtube.com/"),
        ("https://music.yandex.ru/search?text=a#b", "https://music.yandex.ru/"),
        ("browser://extensions", ""),
        ("", ""),
    ],
)
def test_home_page_of_a_site(url: str, expected: str) -> None:
    """«Перейди на главную» — это адрес сайта без пути, а не нажатие логотипа."""
    assert page.origin_of(url) == expected


async def test_home_goes_in_the_same_tab(loaded) -> None:
    """Главная открывается в этой же вкладке: новая тут никому не нужна.

    Логотип нажать не получается — это ссылка, а список для модели собирается
    из кнопок, и его там нет. Живой случай: «нажми на лого YouTube» дважды
    упёрлось в «модель не нашла кнопку».
    """
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()

    result = await registry.invoke("page.home", {})

    assert result.ok
    assert [call[0] for call in fake.CALLS] == ["target", "go"]
    assert fake.CALLS[1][1] == "https://music.yandex.ru/"
    assert fake.CALLS[1][2] == fake.TARGET["tabId"], "вкладка та же"
    await manager.stop()


@pytest.mark.parametrize(
    ("spoken", "way"),
    [
        ("вверх", "up"),
        ("страницу вверх", "up"),
        ("вниз", "down"),
        ("ниже", "down"),
        ("начало", "top"),
        ("конец", "bottom"),
        ("в самый низ", "bottom"),
        ("down", "down"),
        ("боком", ""),
    ],
)
def test_scroll_direction_is_understood(spoken: str, way: str) -> None:
    """Куда листать — из услышанного, а не из значения по умолчанию.

    Живой случай: «пролистай страницу вверх» модель разобрала как перемотку
    назад — своей команды для листания не было вовсе.
    """
    assert page.scroll_way(spoken) == way


def test_scroll_step_takes_only_known_directions() -> None:
    """Направление — данные из закрытого списка, как и всё, что уходит в страницу."""
    # В шаг уходит английское слово, как и у всех остальных глаголов: русское
    # «вверх» превращает в него `scroll_way`, до плана дело ещё не дошло.
    assert page.validate_plan([{"scroll": " Up "}]) == [{"scroll": "up"}]
    assert page.validate_plan([{"scroll": "вверх"}]) == []
    assert page.validate_plan([{"scroll": "куда-нибудь"}]) == []


async def test_scroll_reaches_the_page(loaded) -> None:
    """«Пролистай страницу вверх» уходит в ту вкладку, куда смотрят."""
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.REPLIES[:] = [{"done": "scroll", "detail": "up"}]

    result = await registry.invoke("page.scroll_page", {"where": "вверх"})

    assert result.ok
    assert result.speech_for("ru") == "Пролистал вверх."
    assert fake.CALLS[0] == ("target", "", True), "листают там, куда смотрят"
    assert fake.CALLS[1][1] == [{"scroll": "up"}]
    await manager.stop()


def test_toggle_never_means_next_track() -> None:
    """У модели про переключение больше не спрашивают.

    Живой промах: «переключить воспроизведение» она поняла как «следующий
    трек» и выбрала на Яндекс Музыке «Следующая песня» — это запомнилось
    навсегда и потом мешало. Спрашивать тут не о чем: у переключения есть и
    общий способ (сам плеер), и подписи кнопок.
    """
    assert not page.learnable("toggle")
    assert page.ACTIONS["toggle"][0] == {"media": "toggle"}
    labels = page.ACTIONS["toggle"][1]["label"]
    assert "пауза" in labels and "воспроизвести" in labels
    assert not any("следующ" in word for word in labels)
    assert "следующий" not in page.INTENT_TEXT["toggle"]


def test_typed_text_is_a_string_not_a_list() -> None:
    """Печатать — это текст, и он уходит в значение поля, а не в код."""
    plan = page.validate_plan([{"type": "  don't stop me now  ", "submit": True}])

    assert plan == [{"type": "don't stop me now", "submit": True}]
    assert page.validate_plan([{"type": "   "}]) == []
    assert page.validate_plan([{"type": "a" * (page.MAX_TEXT + 1)}]) == []


def test_typed_text_loses_the_tail() -> None:
    """«Введи в поиск X на сайте» — «на сайте» сказано человеку, не полю."""
    assert page.clean_text("«Don't Stop Me Now» на сайте") == "Don't Stop Me Now"


async def test_page_search_is_not_a_web_search(loaded) -> None:
    """«Введи в поиск …» печатает на странице, а не открывает поисковик.

    Живой случай: на Яндекс Музыке нажали «поиск», сказали «введи в поиск
    Don't Stop Me Now» — и открылась выдача Яндекса. «Найди на Яндекс Музыке» и
    «загугли» — разные просьбы.
    """
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.REPLIES[:] = [{"done": "type", "detail": "don't stop me now"}]

    result = await registry.invoke("page.type_in", {"text": "Don't Stop Me Now"})

    assert result.ok
    assert [call[0] for call in fake.CALLS] == ["target", "run"], "поисковик не открываем"
    step = fake.CALLS[1][1][0]
    assert step == {"type": "Don't Stop Me Now", "submit": True}
    await manager.stop()


def test_rejected_button_leaves_the_plan() -> None:
    """Что уже пробовали и что оказалось не тем, из плана вычитается.

    Селектор выбрасывается, подпись уходит в запреты: второй раз нажимать ту
    же не ту кнопку незачем.
    """
    plan = page.without_rejected(
        [
            {"click": ["#like", "#segmented-like-button"]},
            {"click": ["#like"]},
            {"label": ["нравится"], "avoid": ["не нравится"]},
        ],
        [{"name": "Нравится", "sel": "#like"}],
    )

    assert plan == [
        {"click": ["#segmented-like-button"]},
        {"label": ["нравится"], "avoid": ["не нравится", "нравится"]},
    ]


def test_amount_lands_in_media_steps() -> None:
    """Секунды перемотки и шаг громкости подставляются в медиа-шаги."""
    plan = page.with_amount(
        [{"media": "forward"}, {"media": "louder"}, {"label": ["дальше"]}],
        seconds=30,
        step=0.2,
    )
    assert plan[0] == {"media": "forward", "seconds": 30.0}
    assert plan[1] == {"media": "louder", "amount": 0.2}
    assert plan[2] == {"label": ["дальше"]}


# --- выбор кнопки моделью ---------------------------------------------------


def test_choice_is_a_number_from_the_list() -> None:
    """Модель называет номер, а не селектор: выдумать номер она не может."""
    controls = [{"name": "Пауза", "sel": "#p"}, {"name": "Следующий трек", "sel": "#n"}]
    assert page.choose_control("2", controls)["sel"] == "#n"
    assert page.choose_control("Кнопка 1 подойдёт", controls)["name"] == "Пауза"
    assert page.choose_control("0", controls) is None
    assert page.choose_control("17", controls) is None
    assert page.choose_control("не знаю", controls) is None


def test_control_plan_keeps_label_as_backup() -> None:
    """Рядом с селектором сохраняется подпись: она переживёт редизайн."""
    plan = page.control_plan({"name": "Следующий трек", "sel": '[data-test-id="NEXT"]'})
    assert plan == [
        {"click": ['[data-test-id="NEXT"]']},
        {"label": ["следующий трек"]},
    ]


def test_control_plan_without_selector() -> None:
    """Кнопка без устойчивого селектора остаётся узнаваемой по подписи."""
    assert page.control_plan({"name": "Нравится", "sel": ""}) == [{"label": ["нравится"]}]


def test_model_is_asked_only_about_buttons() -> None:
    """У модели спрашивают лишь о том, что вообще есть в списке кнопок.

    «Открой первое видео» — это ссылка в выдаче, а список собирается из кнопок.
    На живом YouTube модель в ответ предложила деление шкалы времени, и это
    ушло в память как способ включить видео.
    """
    assert page.learnable("like")
    assert page.learnable("press:поделиться")
    assert not page.learnable("first")


@pytest.mark.parametrize(
    ("name", "asked", "alike"),
    [
        ("Копировать ссылку", "скопировать", True),
        ("Поделиться", "поделиться", True),
        ("YouTube Главная", "логотип youtube", True),
        ("Ещё", "скопировать", False),
        ("Настройки", "подписаться", False),
    ],
)
def test_named_button_choice_is_checked(name: str, asked: str, alike: bool) -> None:
    """Если кнопку назвал владелец, выбор модели проверяется по его же словам.

    Угадывать тут нечего: сказанное **и есть** подпись. На живом YouTube на
    «нажми кнопку скопировать» модель выбрала «Ещё» — и это запомнилось.
    """
    assert page.resembles(name, asked) is alike


def test_declined_button_name_still_matches() -> None:
    """Название кнопки склоняют: «нажми кнопку коллекции» → «Коллекция».

    Сравнение идёт началом слова, поэтому достаточно отбросить окончание —
    одна основа покрывает все падежи. Живой случай на Яндекс Музыке.
    """
    variants = page.label_variants("кнопку коллекции на сайте")

    assert variants[0] == "коллекции"
    assert "коллекц" in variants, "основа нужна, иначе падеж не найдётся"
    # Коротким словам основа не нужна и вредна: от «лайк» осталось бы «лай».
    assert page.label_variants("лайк") == ["лайк", "layk"]


def test_service_words_are_stripped_from_the_label() -> None:
    """«Кнопку» спереди и «на сайте» сзади сказаны человеку, а не странице."""
    assert page.label_variants("кнопку поделиться на сайте")[0] == "поделиться"
    assert page.label_variants("на логотип YouTube на странице")[0] == "логотип youtube"
    assert page.label_variants("кнопку") == []


def test_label_variants_add_latin() -> None:
    """Услышанное сравнивается и в латинской записи: алфавит выбирает Whisper."""
    assert page.label_variants(" «Подписаться» ") == [
        "подписаться",
        "подписатьс",
        "podpisatsya",
    ]
    assert page.label_variants("Subscribe") == ["subscribe"]
    assert page.label_variants("   ") == []


# --- скилл целиком ----------------------------------------------------------

_FAKE_BROWSER = '''
"""Подделка браузера: запоминает планы и отвечает заготовками."""

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import Skill, SkillMeta
from jarvis.core.tools import tool

CALLS = []
TARGET = {"tabId": 7, "url": "https://music.yandex.ru/home", "title": "Моя волна"}
# Вкладки названных сайтов: «включи трек X» умеет уходить на музыкальный сайт.
SITE_TARGETS = {}
REPLIES = []
CONTROLS = []
MISSING = []


class FakeBrowserSkill(Skill):
    """Только те инструменты, которыми пользуется скилл страницы."""

    meta = SkillMeta(name="browser", description="Подделка браузера")

    @tool(routable=False)
    async def page_target(self, site: str = "", active: bool = False) -> ToolResult:
        """Вкладка, к которой относится команда."""
        CALLS.append(("target", site, active))
        if site and site in MISSING:
            return ToolResult.failure("нет подходящей вкладки")
        if site and site in SITE_TARGETS:
            return ToolResult.success(dict(SITE_TARGETS[site]))
        return ToolResult.success(dict(TARGET))

    @tool(routable=False)
    async def page_run(self, plan: list[dict], site: str = "", tab: int = 0) -> ToolResult:
        """Выполнить план в странице."""
        CALLS.append(("run", plan, tab))
        reply = REPLIES.pop(0) if REPLIES else {"done": None}
        return ToolResult.success(dict(reply))

    @tool(routable=False)
    async def page_go(self, url: str, tab: int = 0) -> ToolResult:
        """Увести вкладку по другому адресу."""
        CALLS.append(("go", url, tab))
        return ToolResult.success({"tabId": tab, "url": url})

    @tool(routable=False)
    async def page_probe(self, site: str = "", tab: int = 0, limit: int = 40) -> ToolResult:
        """Кнопки страницы."""
        CALLS.append(("probe", tab))
        return ToolResult.success({"controls": [dict(item) for item in CONTROLS]})

    @tool()
    async def open_site(self, site: str = "") -> ToolResult:
        """Открыть сайт."""
        CALLS.append(("open", site))
        return ToolResult.success({"url": site})

    @tool()
    async def search(self, query: str, engine: str = "") -> ToolResult:
        """Открыть выдачу поиска."""
        CALLS.append(("search", query, engine))
        return ToolResult.success({"query": query, "engine": engine})
'''


class _Answer:
    """Провайдер, отвечающий заранее заданной строкой."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.asked: list[str] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def configured(self) -> bool:
        return True

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.asked.append(request.messages[-1].content)
        return LLMResponse(text=self.text, model="fake")

    async def aclose(self) -> None:
        return None


@pytest.fixture
def provider() -> _Answer:
    """Модель, которая всегда выбирает вторую кнопку."""
    return _Answer("2")


@pytest.fixture
def smart_llm(provider: _Answer) -> LLMService:
    """Сервис LLM с настроенным провайдером — иначе обучение не начнётся."""
    profile = TaskProfile(task="intent", provider="fake", model="stub")
    return LLMService(
        providers={"fake": provider},
        profiles=ProfileRegistry({"intent": profile}, default_task="intent"),
    )


@pytest.fixture
def sites_memory(tmp_path: Path):
    """Память с разделом ``sites``: туда уходит выученное."""
    return build_memory(
        MemoryConfig(
            dir=tmp_path / "memory",
            documents=("profile", "sites"),
            journals=("today",),
            context_budget_tokens=500,
        )
    )


@pytest.fixture
def loaded(tmp_path: Path, sites_memory, smart_llm: LLMService):
    """Каталог с настоящим скиллом страницы и подделкой браузера рядом."""
    directory = tmp_path / "skills"
    directory.mkdir()
    shutil.copy(_ROOT / "skills" / "browser" / "page" / "skill.py", directory / "page.py")
    (directory / "browser.py").write_text(_FAKE_BROWSER, encoding="utf-8")

    events = LocalEventBus()
    registry = ToolRegistry(events=events, default_timeout=2.0)
    manager = SkillManager(
        config=SkillsConfig(paths=(directory,)),
        events=events,
        tools=registry,
        memory=sites_memory,
        llm=smart_llm,
        tts=NullTTS(),
        root=tmp_path,
    )
    return manager, registry, sites_memory


async def test_pause_reaches_the_playing_tab(loaded) -> None:
    """«Пауза» уходит в найденную вкладку одним планом, без вопросов к модели."""
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.REPLIES.append({"done": "media", "detail": "пауза", "url": fake.TARGET["url"]})

    result = await registry.invoke("page.pause")

    assert result.ok
    assert result.speech_for("ru") == "Пауза."
    kinds = [call[0] for call in fake.CALLS]
    assert kinds == ["target", "run"], "лишние обращения к браузеру"
    plan = fake.CALLS[1][1]
    assert plan[0] == {"media": "pause"}, "плеер пробуется первым"
    assert fake.CALLS[1][2] == 7, "работаем в найденной вкладке, а не в какой попало"
    await manager.stop()


async def test_site_recipe_goes_first(loaded) -> None:
    """На знакомом сайте первым пробуется его рецепт, а не общий способ."""
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.REPLIES.append({"done": "click", "detail": "Следующий трек"})

    result = await registry.invoke("page.next_track")

    assert result.ok
    plan = fake.CALLS[1][1]
    assert plan[0] == {"click": ['[data-test-id="NEXT_TRACK_BUTTON"]']}
    assert {"label": page.ACTIONS["next"][0]["label"]} in plan, "общий способ остаётся запасным"
    await manager.stop()


async def test_learned_once_and_remembered(loaded, provider: _Answer) -> None:
    """Не сработало — спрашиваем модель один раз, дальше берём из памяти."""
    manager, registry, memory = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.CONTROLS[:] = [
        {"name": "Пауза", "sel": "#pause"},
        {"name": "Мне нравится", "sel": '[data-test-id="LIKE"]'},
    ]
    # Первый план не сработал, а нажатие выбранной кнопки — да.
    fake.REPLIES[:] = [{"done": None}, {"done": "click", "detail": "Мне нравится"}]

    first = await registry.invoke("page.like")

    assert first.ok
    assert [call[0] for call in fake.CALLS] == ["target", "run", "probe", "run"]
    assert "Мне нравится" in provider.asked[0], "модели показывают кнопки страницы"

    saved = await memory.documents.get("sites", "music.yandex.ru")
    assert saved["actions"]["like"] == [
        {"click": ['[data-test-id="LIKE"]']},
        {"label": ["мне нравится"]},
    ]

    # Второй раз тот же вопрос модели не задаётся: способ уже известен.
    fake.CALLS.clear()
    provider.asked.clear()
    fake.REPLIES[:] = [{"done": "click", "detail": "Мне нравится"}]

    second = await registry.invoke("page.like")

    assert second.ok
    assert [call[0] for call in fake.CALLS] == ["target", "run"]
    assert provider.asked == []
    assert fake.CALLS[1][1][0] == {"click": ['[data-test-id="LIKE"]']}
    await manager.stop()


async def test_learned_recipe_can_be_forgotten(loaded, provider: _Answer) -> None:
    """«Не сохраняй в память» отменяет и выученную кнопку.

    Именно этот случай и просили: сработать могло, а нажаться — не то, что
    нужно. Ядро находит этот скилл по имени инструмента `forget_last`.
    """
    manager, registry, memory = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.CONTROLS[:] = [{"name": "Пауза", "sel": "#pause"}, {"name": "Нравится", "sel": "#like"}]
    fake.REPLIES[:] = [
        {"done": None},
        {"done": "click", "detail": "Нравится", "sel": "#like"},
    ]

    await registry.invoke("page.like")
    assert (await memory.documents.get("sites", "music.yandex.ru"))["actions"]

    forgotten = await registry.invoke("page.forget_last")

    assert forgotten.ok
    assert forgotten.value == "like на music.yandex.ru: Нравится"
    saved = await memory.documents.get("sites", "music.yandex.ru")
    assert saved["actions"] == {}
    # И сама кнопка попала в «сюда больше не надо».
    assert saved["rejected"]["like"] == [{"name": "Нравится", "sel": "#like"}]
    # Забывать второй раз нечего.
    assert (await registry.invoke("page.forget_last")).value == ""
    await manager.stop()


async def test_unrelated_choice_is_refused_not_remembered(loaded, provider: _Answer) -> None:
    """Модель предложила кнопку не про то — отказ, и в памяти ничего.

    Живой случай: «нажми кнопку скопировать» на YouTube, кнопки с такой
    подписью на виду нет, и модель выбрала «Ещё». Раньше это нажималось и
    запоминалось навсегда.
    """
    manager, registry, memory = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.CONTROLS[:] = [{"name": "Ещё", "sel": "#more"}]
    fake.REPLIES[:] = [{"done": None}]
    provider.text = "1"

    result = await registry.invoke("page.press", {"control": "скопировать"})

    assert not result.ok
    assert [call[0] for call in fake.CALLS] == ["target", "run", "probe"]
    assert not (await memory.documents.get("sites", "music.yandex.ru", {})).get("actions")
    await manager.stop()


async def test_link_actions_do_not_ask_the_model(loaded, provider: _Answer) -> None:
    """Про «первое видео» модель не спрашивают: в списке одни кнопки.

    На живом YouTube она предложила деление шкалы времени, и это ушло в память
    как способ включить видео.
    """
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.CONTROLS[:] = [{"name": "0 мин. 29 сек.", "sel": "#bar"}]
    fake.REPLIES[:] = [{"done": None}]
    provider.asked.clear()

    result = await registry.invoke("page.open_first")

    assert not result.ok
    assert provider.asked == [], "модель спрашивать не о чем"
    assert "probe" not in [call[0] for call in fake.CALLS]
    await manager.stop()


async def test_next_time_another_button_is_tried(loaded, provider: _Answer) -> None:
    """Отменённая кнопка не предлагается снова — ни в плане, ни модели.

    Это и есть «попробуй другую»: второй раз то же действие идёт мимо
    отвергнутого, а модель видит список кнопок уже без него.
    """
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CONTROLS[:] = [
        {"name": "Нравится", "sel": "#like"},
        {"name": "В коллекцию", "sel": "#collect"},
    ]
    fake.REPLIES[:] = [
        {"done": None},
        {"done": "click", "detail": "Нравится", "sel": "#like"},
    ]
    await registry.invoke("page.like")
    await registry.invoke("page.forget_last")

    fake.CALLS.clear()
    provider.asked.clear()
    # В списке осталась одна кнопка — та, что не отвергнута.
    provider.text = "1"
    fake.REPLIES[:] = [
        {"done": None},
        {"done": "click", "detail": "В коллекцию", "sel": "#collect"},
    ]

    second = await registry.invoke("page.like")

    assert second.ok
    plan = fake.CALLS[1][1]
    assert all("#like" not in step.get("click", []) for step in plan)
    assert "нравится" in plan[-1].get("avoid", []), "подпись ушла в запреты"
    assert "Нравится" not in provider.asked[0], "отвергнутую кнопку модели не показываем"
    assert "В коллекцию" in provider.asked[0]
    await manager.stop()


async def test_nothing_worked_is_an_honest_refusal(loaded) -> None:
    """Ни один способ не подошёл и модель не помогла — честный отказ."""
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.CONTROLS.clear()
    fake.REPLIES.clear()

    result = await registry.invoke("page.press", {"control": "Подписаться"})

    assert not result.ok
    assert "music.yandex.ru" in result.error
    await manager.stop()


async def test_closed_site_still_pauses(loaded) -> None:
    """«Поставь ютуб на паузу» при закрытом ютубе останавливает то, что звучит.

    Название сайта тут пожелание, а не требование: остановить просят звук, и
    молчать из-за неудачно названной вкладки хуже, чем выполнить.
    """
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.MISSING[:] = ["ютуб"]
    fake.REPLIES[:] = [{"done": "media", "detail": "пауза"}]

    result = await registry.invoke("page.pause", {"site": "ютуб"})

    assert result.ok
    assert [call[:2] for call in fake.CALLS[:2]] == [("target", "ютуб"), ("target", "")]
    fake.MISSING.clear()
    await manager.stop()


async def test_press_does_not_wander_to_another_site(loaded) -> None:
    """А вот кнопку на чужом сайте нажимать нельзя — тут отказ.

    Разница принципиальна: пауза относится к звуку, а нажатие — к странице, и
    промах здесь означает нажатую не ту кнопку не там.
    """
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.MISSING[:] = ["ютуб"]

    result = await registry.invoke("page.press", {"control": "подписаться", "site": "ютуб"})

    assert not result.ok
    assert [call[0] for call in fake.CALLS] == ["target"]
    fake.MISSING.clear()
    await manager.stop()


async def test_first_video_looks_where_you_look(loaded) -> None:
    """«Включи первое видео» относится к вкладке в фокусе, а не к звучащей.

    Команда идёт следом за поиском, а музыка в это время может играть в
    соседнем окне — нажимать надо там, куда смотрят.
    """
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.TARGET["url"] = "https://www.youtube.com/results?search_query=мегамозг"
    fake.REPLIES[:] = [{"done": "click", "detail": "Мегамозг, трейлер"}]

    result = await registry.invoke("page.open_first")

    assert result.ok
    assert fake.CALLS[0] == ("target", "", True), "нужна вкладка в фокусе"
    assert fake.CALLS[1][1][0]["click"][0].startswith("ytd-video-renderer")
    fake.TARGET["url"] = "https://music.yandex.ru/home"
    await manager.stop()


async def test_pause_follows_the_sound(loaded) -> None:
    """А «пауза», наоборот, идёт туда, откуда звук."""
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.REPLIES[:] = [{"done": "media", "detail": "пауза"}]

    await registry.invoke("page.pause")

    assert fake.CALLS[0] == ("target", "", False)
    await manager.stop()


async def test_open_video_is_not_a_program(loaded) -> None:
    """«Открой видео …» — это ролик, а не программа.

    Живой случай: «открой видео, как Ян Топлис обманывал всех 10 лет на сайте»
    уходило в запуск программ — фраза начинается с «открой», и шаблон
    «открой {program}» её забирал.
    """
    from jarvis.core.contracts import Utterance
    from jarvis.core.router import PhraseResolver

    manager, registry, _ = loaded
    await manager.start()

    intent = await PhraseResolver(registry).resolve(
        Utterance(text="открой видео как Ян Топлис обманывал всех 10 лет на сайте")
    )

    assert intent is not None
    assert intent.tool == "page.play_video"
    # «На сайте» сказано человеку — в запрос оно попадать не должно.
    assert intent.arguments["track"] == "как Ян Топлис обманывал всех 10 лет"
    await manager.stop()


async def test_video_goes_to_the_video_site(loaded, monkeypatch) -> None:
    """«Включи видео X» ищет ролик, а не музыку.

    Фразы про видео жили в скилле youtube, а владелец его удалил. Забрать их
    было обязательно: «открой видео Мегамозг» иначе снова достаётся шаблону
    «открой {program}» из запуска программ — а это ровно тот случай, когда голос
    вызвал запрос прав администратора.
    """
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    monkeypatch.setattr(sys.modules["jarvis_skills.page"], "SEARCH_DELAY", 0)
    monkeypatch.setattr(
        fake, "TARGET", {"tabId": 7, "url": "https://example.com/", "title": "Ничего"}
    )
    monkeypatch.setitem(
        fake.SITE_TARGETS,
        "ютуб",
        {"tabId": 5, "url": "https://www.youtube.com/", "title": "YouTube"},
    )
    fake.CALLS.clear()
    fake.REPLIES[:] = [
        {"done": None},
        {"done": None},
        {"done": "item", "detail": "мегамозг", "played": True},
    ]

    result = await registry.invoke("page.play_video", {"track": "трейлер мегамозг"})

    assert result.ok
    assert ("open", "ютуб") in fake.CALLS, "музыкальный сайт тут не при чём"
    kinds = [call[0] for call in fake.CALLS]
    assert fake.CALLS[kinds.index("go")][1].startswith("https://www.youtube.com/results")
    await manager.stop()


async def test_press_uses_spoken_label(loaded) -> None:
    """«Нажми подписаться» ищет кнопку по подписи, а не по рецепту."""
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.REPLIES[:] = [{"done": "label", "detail": "подписаться"}]

    result = await registry.invoke("page.press", {"control": "Подписаться"})

    assert result.ok
    assert fake.CALLS[1][1] == [
        {"label": ["подписаться", "подписатьс", "podpisatsya"]}
    ]
    await manager.stop()
