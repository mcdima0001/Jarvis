"""Страница: сделать во вкладке то, что обычно делают мышкой.

**Подскилл браузера** — лежит внутри него намеренно (`skills/browser/page/`).
Он зовёт `browser.page_run` и без браузера бесполезен, поэтому и грузится после
него, а без него не грузится вовсе. Соседом по общему списку он выглядел как
вторая независимая возможность «для браузера» — а это неправда.

Почему всё-таки отдельный скилл, а не часть `skill.py` браузера: у них разные
зависимости и разные причины отказа. Браузер работает **без** расширения —
ссылку открывает система; страница без расширения не может ничего в принципе.
Адреса и вкладки живут годами, а кнопки внутри страниц меняются вместе с
сайтами, и растёт этот файл вместе с ними.

**Обращение к модели тут — исключение, а не способ работы.** Порядок такой:

1. фраза узнаётся шаблоном и превращается в действие — бесплатно и мгновенно;
2. действие превращается в план: свои рецепты из конфига, выученное из памяти,
   встроенный рецепт сайта, общий способ. Всё это данные, а не запросы;
3. и только если ни один вариант не сработал, страница один раз описывается
   модели («вот кнопки, что нажать?»), а её выбор **уходит в память** — для
   этого сайта и этого действия вопрос больше не задаётся никогда.

Общий способ работает почти везде и без всяких рецептов, потому что опирается
не на разметку, а на две вещи, которые есть у всех: сам плеер (`<video>` и
`<audio>` умеют play/pause/громкость/перемотку из коробки) и подписи кнопок —
те самые, что читает экранный диктор. Разметку сайты переделывают каждый год,
а «Следующий трек» на кнопке остаётся.

Безопасность здесь та же, что и во всём проекте: **из голоса в страницу уходят
только данные**. Селектор попадает в `querySelector`, подпись — в сравнение
строк. Исполняемый код лежит в `extension/page.js`, приходит из расширения и
никогда — из речи или от модели. Набор шагов закрытый, и всё, что в него не
уложилось, отбрасывается ещё в Python (`validate_plan`), а потом ещё раз в
самой странице.
"""

from __future__ import annotations

import asyncio
import difflib
import re
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence
from urllib.parse import quote_plus, urlsplit

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.text import romanize, skeleton, squash
from jarvis.core.tools import tool

#: Что умеет сам плеер, без единой кнопки сайта.
MEDIA_ACTIONS = frozenset(
    {
        "play",
        "pause",
        "toggle",
        "mute",
        "unmute",
        "louder",
        "quieter",
        "forward",
        "back",
    }
)

#: Глаголы шага. Список закрытый: чего тут нет, того странице не отправить.
#: У `type` и `scroll` значение — строка, у остальных — список.
STEP_VERBS = ("media", "label", "click", "item", "type", "scroll")

#: Куда листать страницу. Тоже закрытый список — это данные, не код.
SCROLL_WAYS = frozenset({"up", "down", "top", "bottom"})

#: Как называется кнопка воспроизведения внутри строки трека. Нужно шагу
#: `item`: нажатие по самой строке трек только выбирает, играть начинает она.
PLAY_HINTS = ("воспроизв", "слушать", "включить", "play", "listen")

#: Сколько вариантов имеет смысл перебирать. Столько же режет и page.js.
MAX_STEPS = 8

#: Ограничения на строки внутри шага: это данные из памяти и от модели.
MAX_SELECTORS = 6
MAX_TEXT = 200

#: Запретов бывает больше, чем селекторов: к встроенным добавляются подписи
#: кнопок, которые владелец уже отверг. Общий предел обрезал бы как раз их —
#: они дописываются последними.
MAX_AVOID = 16

ACTION = Literal[
    "play",
    "pause",
    "toggle",
    "next",
    "previous",
    "like",
    "unlike",
    "dislike",
    "mute",
    "unmute",
    "louder",
    "quieter",
    "forward",
    "back",
    "first",
]

#: Как выполнять действие на любом сайте.
#:
#: Порядок внутри — от надёжного к общему. Сначала плеер: он не зависит от
#: разметки вовсе. Потом подписи кнопок — они переживают редизайн, потому что
#: их читает экранный диктор, и менять их просто так никто не станет.
#:
#: Подписи сравниваются началом слова, а не любым куском: «like» внутри
#: «dislike» стоило бы дизлайка вместо лайка. Одного этого мало — по-русски
#: отрицание стоит **перед** словом («не нравится» содержит «нравится» целиком
#: и словом, и краем), поэтому у шага есть ещё и список запретных подписей.
ACTIONS: dict[str, tuple[dict[str, Any], ...]] = {
    "play": (
        {"media": "play"},
        {"label": ["воспроизвести", "слушать", "смотреть", "play", "включить"]},
    ),
    "pause": (
        {"media": "pause"},
        {"label": ["пауза", "приостановить", "pause"]},
    ),
    # У переключения общий способ есть всегда — сам плеер. Подписи идут вторыми
    # и обе сразу: «переключить» — это либо включить, либо остановить.
    "toggle": (
        {"media": "toggle"},
        {"label": ["воспроизвести", "слушать", "play", "пауза", "pause"]},
    ),
    "next": (
        {"label": ["следующий", "следующая", "следующее", "next track", "next video", "next song"]},
    ),
    "previous": (
        {"label": ["предыдущий", "предыдущая", "предыдущее", "previous"]},
    ),
    # Кнопок-близнецов тут три: лайк, дизлайк и «убрать лайк». Отличаются они
    # одним словом, и это слово — отрицание, поэтому запреты важнее совпадений.
    "like": (
        {
            "label": ["нравится", "лайк", "like"],
            "avoid": [
                "не нравится",
                "убрать",
                "отменить",
                "снять",
                "dislike",
                "remove",
                "undo",
            ],
        },
    ),
    # Снять лайк — это нажать ту же кнопку ещё раз, но подпись у неё уже
    # другая: «Убрать отметку „Нравится“». Отдельное действие, а не «лайк
    # наоборот».
    "unlike": (
        {
            "label": ["убрать отметку", "убрать лайк", "убрать из", "unlike", "remove like"],
            "avoid": ["не нравится", "dislike"],
        },
    ),
    "dislike": (
        {
            "label": ["не нравится", "дизлайк", "dislike"],
            "avoid": ["убрать", "отменить", "снять", "remove", "undo"],
        },
    ),
    # Первый результат поиска или первый ролик в подборке. Общего способа тут
    # нет и быть не может: у каждого сайта своя разметка выдачи, поэтому
    # работает по рецепту сайта либо по выученному.
    "first": (),
    "mute": ({"media": "mute"}, {"label": ["выключить звук", "mute", "без звука"]}),
    "unmute": ({"media": "unmute"}, {"label": ["включить звук", "unmute"]}),
    "louder": ({"media": "louder"},),
    "quieter": ({"media": "quieter"},),
    "forward": ({"media": "forward"},),
    "back": ({"media": "back"},),
}

#: Рецепты под конкретные сайты — на случай, когда подписи не хватает.
#:
#: Это подсказка, а не требование: селектор устаревает вместе с редизайном, и
#: тогда план просто идёт дальше, к общему способу. Поэтому неверный селектор
#: тут не ломает команду, а лишь перестаёт помогать.
SITE_RECIPES: dict[str, dict[str, tuple[dict[str, Any], ...]]] = {
    "youtube.com": {
        "next": ({"click": [".ytp-next-button"]},),
        "previous": ({"click": [".ytp-prev-button"]},),
        "like": (
            {"click": ["like-button-view-model button", "#segmented-like-button button"]},
        ),
        "first": (
            {
                "click": [
                    "ytd-video-renderer a#video-title",
                    "ytd-rich-item-renderer a#video-title-link",
                    "a#video-title",
                ]
            },
        ),
    },
    "music.yandex.ru": {
        "next": ({"click": ['[data-test-id="NEXT_TRACK_BUTTON"]']},),
        "previous": ({"click": ['[data-test-id="PREV_TRACK_BUTTON"]']},),
        "play": ({"media": "play"}, {"click": ['[data-test-id="PLAY_BUTTON"]']}),
        "pause": ({"media": "pause"}, {"click": ['[data-test-id="PAUSE_BUTTON"]']}),
        "like": ({"click": ['[data-test-id="LIKE_BUTTON"]']},),
        # Верхний результат выдачи — большая карточка с обложкой. Хвост класса
        # (`__rV9pQ`) меняется при каждой сборке сайта, поэтому сравниваем
        # куском: имя из CSS-модуля переживает пересборку, случайный хвост нет.
        "first": ({"click": ['[class*="PlayButtonWithCover_playButton"]']},),
    },
    "vk.com": {
        "next": ({"click": [".audio_page_player_next"]},),
        "previous": ({"click": [".audio_page_player_prev"]},),
    },
    # Первая ссылка в выдаче. Разметка поисковиков меняется чаще всего, поэтому
    # вариантов по несколько: не подошёл ни один — останется спросить у модели
    # и запомнить ответ, как и на любом незнакомом сайте.
    "yandex.ru": {
        "first": (
            {"click": ["li.serp-item a.OrganicTitle-Link", ".serp-item h2 a", ".Organic-Title"]},
        ),
    },
    "google.com": {"first": ({"click": ["#rso h3", "#search h3"]},)},
}

#: Где у сайта своя выдача: адрес поиска с подстановкой запроса.
#:
#: Нужно для «включи Don't Stop Me Now»: на странице этого трека нет, потому что
#: его никто не искал. Ссылкой, а не набором текста в строке поиска, — по той же
#: причине, по которой так устроен и `browser.search`: результат тот же, а
#: промахнуться нечем. Открывается **в той же вкладке** (`browser.page_go`):
#: новая была бы лишней, работа идёт там, куда смотрит владелец.
SITE_SEARCH: dict[str, str] = {
    "music.yandex.ru": "https://music.yandex.ru/search?text={query}",
    "youtube.com": "https://www.youtube.com/results?search_query={query}",
    "vk.com": "https://vk.com/audio?q={query}",
}

#: Сколько раз перепроверить страницу после перехода на выдачу.
#:
#: `loaded()` в расширении ждёт события браузера, но выдача у таких сайтов
#: дорисовывается скриптом уже после него: разметка есть, треков ещё нет.
#: Приложение сайта дорисовывает выдачу уже после события загрузки, и сколько
#: это займёт, зависит от сети. Шесть попыток по 0.7 с — это около четырёх
#: секунд ожидания, и все они честные: каждая смотрит на живой DOM.
SEARCH_ATTEMPTS = 6
SEARCH_DELAY = 0.7


def silent(result: Mapping[str, Any]) -> bool:
    """Нажали, но звук не появился.

    Расширение отвечает `played: false`, когда «включи» дошло до конца и
    ничего не заиграло. Это не успех: отчитаться «включаю» в тишину — ровно та
    ложь, за которую поправлен свободный разговор.
    """
    return result.get("done") == "item" and result.get("played") is False


def search_url_for(host: str, query: str) -> str:
    """Адрес поиска по сайту — или пусто, если своей выдачи у него нет."""
    template = ""
    for name, pattern in SITE_SEARCH.items():
        if host == name or host.endswith(f".{name}"):
            template = pattern
            break
    if not template or not query.strip():
        return ""
    return template.format(query=quote_plus(query.strip()))

#: Что сказать вслух. Реплики короткие: команда и так видна по результату.
#:
#: Вариантов по нескольку намеренно. Эти фразы звучат чаще всех остальных в
#: проекте — «пауза» десятки раз за вечер, — и одна зашитая строка на слух
#: превращается в сигнал будильника: её перестают слышать. Выбирает вариант
#: персона (`Persona.choose`), она же помнит, что уже говорила.
SPEECH: dict[str, dict[str, tuple[str, ...]]] = {
    "play": {
        "ru": ("Включаю.", "Продолжаю.", "Есть.", "Играет.", "Возвращаю звук."),
        "en": ("Playing.", "Resuming.", "Right away.", "Back on."),
    },
    "pause": {
        "ru": ("Пауза.", "Остановил.", "Ставлю на паузу.", "Тишина.", "Замолчали."),
        "en": ("Paused.", "Stopped.", "Pausing.", "Silence."),
    },
    "toggle": {
        "ru": ("Готово.", "Переключил.", "Сделано."),
        "en": ("Done.", "Toggled.", "There."),
    },
    "next": {
        "ru": ("Следующий.", "Дальше.", "Переключил.", "Следующий трек.", "Идём дальше."),
        "en": ("Next one.", "Moving on.", "Skipped.", "Next up."),
    },
    "previous": {
        "ru": ("Предыдущий.", "Возвращаю.", "Назад.", "Прошлый трек."),
        "en": ("Previous one.", "Going back.", "Back one."),
    },
    "like": {
        "ru": ("Лайкнул.", "Отметил.", "Поставил лайк.", "Записал в понравившееся."),
        "en": ("Liked.", "Noted.", "Marked as liked."),
    },
    "unlike": {
        "ru": ("Убрал лайк.", "Снял отметку.", "Больше не нравится."),
        "en": ("Like removed.", "Unmarked.", "No longer liked."),
    },
    "dislike": {
        "ru": ("Поставил дизлайк.", "Отметил как не понравившееся.", "Больше не предложу."),
        "en": ("Disliked.", "Marked as disliked.", "Won't suggest it again."),
    },
    "first": {
        "ru": ("Включаю.", "Открываю первое.", "Первое из списка.", "Есть, включаю."),
        "en": ("Playing it.", "Opening the first one.", "First on the list."),
    },
    "mute": {
        "ru": ("Заглушил вкладку.", "Звук выключен.", "Вкладка молчит."),
        "en": ("Tab muted.", "Sound off.", "Muted."),
    },
    "unmute": {
        "ru": ("Вернул звук.", "Звук включён.", "Слышно."),
        "en": ("Sound is back.", "Unmuted.", "Audio on."),
    },
    "louder": {
        "ru": ("Громче.", "Добавил громкости.", "Прибавил."),
        "en": ("Louder.", "Turned it up.", "Volume up."),
    },
    "quieter": {
        "ru": ("Тише.", "Убавил.", "Сделал потише."),
        "en": ("Quieter.", "Turned it down.", "Volume down."),
    },
    "forward": {
        "ru": ("Перемотал вперёд.", "Промотал.", "Вперёд."),
        "en": ("Skipped ahead.", "Fast-forwarded.", "Ahead."),
    },
    "back": {
        "ru": ("Перемотал назад.", "Отмотал.", "Назад."),
        "en": ("Skipped back.", "Rewound.", "Back."),
    },
}

#: Чего именно мы хотим — этой строкой действие объясняется модели.
INTENT_TEXT: dict[str, str] = {
    "play": "включить воспроизведение",
    "pause": "поставить на паузу",
    # «Переключить воспроизведение» по-русски читается и как «следующий трек» —
    # модель именно так и поняла, выбрав на Яндекс Музыке «Следующая песня».
    "toggle": "нажать кнопку воспроизведения или паузы",
    "next": "включить следующий трек или видео",
    "previous": "вернуться к предыдущему треку или видео",
    "like": "поставить лайк текущему треку или видео",
    "unlike": "убрать ранее поставленный лайк",
    "dislike": "поставить дизлайк текущему треку или видео",
    "first": "открыть первый результат поиска или первое видео в списке",
    "mute": "выключить звук",
    "unmute": "включить звук",
    "louder": "сделать громче",
    "quieter": "сделать тише",
    "forward": "перемотать вперёд",
    "back": "перемотать назад",
}

#: Действия, для которых у модели вообще есть смысл спрашивать.
#:
#: Список кнопок, который она видит, собирается из `button` и `role=button` —
#: то есть из кнопок. Значит спрашивать можно только про кнопку. «Открой первое
#: видео» — это ссылка в выдаче, её в списке нет и быть не может, и модель в
#: ответ выбирает что попало: на живом YouTube она предложила деление шкалы
#: времени, и это ушло в память как способ включить видео.
#:
#: `toggle` убран отсюда после живого промаха: на Яндекс Музыке модель выбрала
#: «Следующая песня» и это запомнилось навсегда. Спрашивать тут и правда не о
#: чем — у переключения есть общий способ (сам плеер) и подписи кнопок, а если
#: плеера на странице нет, то и переключать нечего.
LEARNABLE = frozenset(
    {"play", "pause", "next", "previous", "like", "unlike", "dislike",
     "mute", "unmute"}
)

#: Насколько подпись выбранной кнопки должна походить на то, что просили нажать.
#: Порог невысокий: «Копировать ссылку» против «скопировать» — это одно и то же.
_LIKENESS = 0.6

#: Слова названия — по ним ищется общее слово у подписи и просьбы.
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


def learnable(action: str) -> bool:
    """Можно ли для этого действия спрашивать у модели, что нажать."""
    return action in LEARNABLE or action.startswith("press:")


def resembles(name: str, wanted: str) -> bool:
    """Похожа ли подпись кнопки на то, что просили нажать.

    Нужно там, где название кнопки назвал сам владелец. Его слова **и есть**
    подпись, и угадывать тут нечего: модель может только промахнуться. На живом
    YouTube на «нажми кнопку скопировать» она выбрала «Ещё» — и это ушло в
    память. Поэтому её выбор проверяется: «Копировать ссылку» на «скопировать»
    похоже, «Ещё» — нет.
    """
    left, right = squash(name), squash(wanted)
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    if skeleton(name) and skeleton(name) == skeleton(wanted):
        return True
    # Общее длинное слово — тоже родство: «логотип YouTube» и «YouTube Главная»
    # говорят про одно и то же, хотя целиком не похожи.
    words = {squash(word) for word in _WORDS.findall(name.lower())}
    asked = {squash(word) for word in _WORDS.findall(wanted.lower())}
    if {word for word in words & asked if len(word) >= 4}:
        return True
    return difflib.SequenceMatcher(None, left, right).ratio() >= _LIKENESS


_LEARN_SYSTEM = (
    "Ты выбираешь кнопку на веб-странице. Отвечай одним числом — номером кнопки "
    "из списка. Если подходящей кнопки нет, ответь 0. Больше ничего не пиши."
)

#: Первое целое число в ответе модели: она любит дописать «Кнопка 3».
_NUMBER = re.compile(r"-?\d+")


def host_of(url: str) -> str:
    """Домен адреса без ``www``: по нему ищется рецепт сайта."""
    try:
        host = urlsplit(str(url)).netloc.lower()
    except ValueError:
        return ""
    return host.split("@")[-1].split(":")[0].removeprefix("www.")


#: Куда листать: как сказали → что отправить странице. Русские слова сравниваются
#: началом, поэтому «вверх», «наверх» и «вверху» попадают в одно и то же.
_SCROLL_WORDS: tuple[tuple[str, str], ...] = (
    ("начало", "top"),
    ("верхн", "top"),
    ("самый низ", "bottom"),
    ("самое низ", "bottom"),
    ("конец", "bottom"),
    ("вверх", "up"),
    ("наверх", "up"),
    ("выше", "up"),
    ("up", "up"),
    ("top", "top"),
    ("вниз", "down"),
    ("ниже", "down"),
    ("down", "down"),
    ("bottom", "bottom"),
)

#: Что сказать вслух про листание.
SCROLL_SPEECH: dict[str, dict[str, tuple[str, ...]]] = {
    "up": {
        "ru": ("Пролистал вверх.", "Поднял выше.", "Вверх."),
        "en": ("Scrolled up.", "Moved up.", "Up."),
    },
    "down": {
        "ru": ("Пролистал вниз.", "Опустил ниже.", "Вниз."),
        "en": ("Scrolled down.", "Moved down.", "Down."),
    },
    "top": {
        "ru": ("В начале страницы.", "Поднял в самое начало.", "Вот начало."),
        "en": ("At the top.", "Back to the top.", "Top of the page."),
    },
    "bottom": {
        "ru": ("В конце страницы.", "Опустил в самый низ.", "Вот конец."),
        "en": ("At the bottom.", "Down to the bottom.", "End of the page."),
    },
}

#: Если у действия своих слов нет вовсе.
_ANYWAY: dict[str, tuple[str, ...]] = {
    "ru": ("Готово.", "Сделано.", "Есть."),
    "en": ("Done.", "All set.", "There."),
}


def scroll_way(spoken: str) -> str:
    """Понять, куда листать. Пусто — не понял."""
    text = " ".join(str(spoken).split()).lower().strip(" «»\"'`.,!?")
    if text in SCROLL_WAYS:
        return text
    for word, way in _SCROLL_WORDS:
        if word in text:
            return way
    return "down" if not text else ""


def origin_of(url: str) -> str:
    """Главная страница сайта: схема и домен без пути.

    Нужно для «перейди на главную»: нажать логотип нельзя — он ссылка, а не
    кнопка, и в списке для модели его нет и не будет.
    """
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}/"


def recipes_for(host: str, catalog: Mapping[str, Any]) -> Mapping[str, Any]:
    """Рецепты, объявленные для этого домена.

    Домен сравнивается с хвоста: рецепт для ``youtube.com`` работает и на
    ``m.youtube.com``, и на ``music.youtube.com``, потому что это тот же сайт
    с той же разметкой.
    """
    if not host:
        return {}
    for name, actions in catalog.items():
        key = str(name).lower().removeprefix("www.")
        if host == key or host.endswith(f".{key}"):
            if isinstance(actions, Mapping):
                return actions
    return {}


def _strings(value: Any, limit: int = MAX_SELECTORS) -> list[str]:
    """Список непустых строк разумной длины — или пусто, если это не он."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    found = [
        str(item).strip()
        for item in value[:limit]
        if isinstance(item, (str, int, float)) and str(item).strip()
    ]
    return [item for item in found if len(item) <= MAX_TEXT]


def validate_plan(raw: Any) -> list[dict[str, Any]]:
    """Оставить от плана только то, что странице разрешено отправлять.

    Планы приходят из трёх мест, и двум из них верить нельзя: конфиг пишет
    человек, память лежит файлом на диске, а выбор кнопки делает языковая
    модель. Поэтому набор глаголов закрытый, строки ограничены по длине, а
    всё непонятое молча отбрасывается — тот же принцип, что и с названиями
    программ: выполняется только узнанное.
    """
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []

    plan: list[dict[str, Any]] = []
    for step in raw:
        if not isinstance(step, Mapping):
            continue
        verb = next((name for name in STEP_VERBS if name in step), "")
        if not verb:
            continue

        if verb == "media":
            value = str(step["media"]).strip().lower()
            if value not in MEDIA_ACTIONS:
                continue
            clean: dict[str, Any] = {"media": value}
            for extra in ("seconds", "amount"):
                number = step.get(extra)
                if isinstance(number, (int, float)) and not isinstance(number, bool):
                    clean[extra] = float(number)
        elif verb == "scroll":
            where = str(step["scroll"]).strip().lower()
            if where not in SCROLL_WAYS:
                continue
            clean = {"scroll": where}
        elif verb == "type":
            # Печатать — это текст, а не список. Он уходит в значение поля, то
            # есть в содержимое страницы: выполнить его нельзя ничем.
            text = str(step["type"]).strip()
            if not text or len(text) > MAX_TEXT:
                continue
            clean = {"type": text}
            into = _strings(step.get("into", ()))
            if into:
                clean["into"] = into
            if step.get("submit"):
                clean["submit"] = True
        else:
            items = _strings(step[verb])
            if not items:
                continue
            clean = {verb: items}
            if verb == "item":
                # Как зовётся кнопка воспроизведения и надо ли дожимать плеер.
                hint = _strings(step.get("hint", ()), limit=MAX_AVOID)
                if hint:
                    clean["hint"] = hint
                if step.get("play"):
                    clean["play"] = True
                # Что взять, не сравнивая названий: верхний результат выдачи.
                prefer = _strings(step.get("prefer", ()))
                if prefer:
                    clean["prefer"] = prefer
            if verb == "label":
                # Запретные слова: подпись «не нравится» содержит «нравится»
                # целиком, и без них лайк оказывается дизлайком.
                avoid = _strings(step.get("avoid", ()), limit=MAX_AVOID)
                if avoid:
                    clean["avoid"] = avoid

        if clean not in plan:
            plan.append(clean)
        if len(plan) >= MAX_STEPS:
            break
    return plan


def merge_plans(*plans: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Склеить варианты по порядку, выбросив повторы."""
    merged: list[dict[str, Any]] = []
    for plan in plans:
        for step in plan or ():
            if step not in merged:
                merged.append(dict(step))
            if len(merged) >= MAX_STEPS:
                return merged
    return merged


#: Сколько отвергнутых кнопок помнить на одно действие. Больше и не нужно: если
#: не подошли пять, дело не в выборе кнопки.
MAX_REJECTED = 5


def without_rejected(
    plan: Sequence[Mapping[str, Any]], rejected: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Убрать из плана то, что уже пробовали и что оказалось не тем.

    Селектор отвергнутой кнопки выбрасывается, её подпись уходит в запреты
    шага `label`. Второй раз нажимать то же самое незачем: пользователь уже
    сказал, что это не то.
    """
    names = [str(item.get("name", "")).strip().lower() for item in rejected]
    names = [name for name in names if name]
    selectors = {str(item.get("sel", "")).strip() for item in rejected}
    selectors.discard("")

    result: list[dict[str, Any]] = []
    for step in plan:
        clean = dict(step)
        if "click" in clean:
            left = [item for item in clean["click"] if item not in selectors]
            if not left:
                continue
            clean["click"] = left
        if "label" in clean and names:
            clean["avoid"] = list(dict.fromkeys([*clean.get("avoid", ()), *names]))
        result.append(clean)
    return result


def with_amount(plan: Sequence[Mapping[str, Any]], *, seconds: float, step: float) -> list[dict]:
    """Подставить в медиа-шаги, на сколько перематывать и насколько менять звук."""
    result: list[dict[str, Any]] = []
    for item in plan:
        clean = dict(item)
        if clean.get("media") in ("forward", "back") and seconds > 0:
            clean["seconds"] = float(seconds)
        if clean.get("media") in ("louder", "quieter") and step > 0:
            clean["amount"] = float(step)
        result.append(clean)
    return result


#: Как договаривают, объясняя, где нажать. В подписи кнопки этого нет никогда,
#: а в аргумент шаблона попадает: «нажми кнопку поделиться **на сайте**».
_TAILS = ("на сайте", "на странице", "в браузере", "на этой странице", "тут", "здесь")

#: И как начинают: «нажми **кнопку** поделиться», «нажми **на** логотип».
_HEADS = ("на кнопку", "кнопку", "кнопка", "иконку", "значок", "на")

#: Гласные на конце — по ним и различаются падежи: «коллекция», «коллекции».
_ENDINGS = "аеёиоуыэюяaeiouy"

#: Короче этого основу не берём: от «лайк» осталось бы «лай».
_MIN_STEM = 6

_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)


def clean_text(spoken: str) -> str:
    """Привести услышанный текст к тому, что печатают: без кавычек и хвостов."""
    text = " ".join(str(spoken).split()).strip(" «»\"'`.,!?")
    for tail in _TAILS:
        if text.lower().endswith(f" {tail}"):
            text = text[: -len(tail) - 1].strip(" ,")
    # Кавычки снимаются ещё раз: закрывающая стоит перед хвостом, а не в конце
    # фразы — «введи в поиск «Don't Stop Me Now» на сайте».
    return text.strip(" «»\"'`.,!?")


def label_variants(spoken: str) -> list[str]:
    """Как подпись кнопки может выглядеть на странице.

    Whisper пишет услышанное одним алфавитом, а на кнопке бывает другой,
    поэтому рядом с услышанным идёт его латинская запись. Перевод не
    подразумевается: «подписаться» и «subscribe» — разные слова, а не разные
    написания одного.

    Заодно снимаются служебные слова вокруг названия: «кнопку» спереди и «на
    сайте» сзади сказаны для человека, а на кнопке их нет.
    """
    text = " ".join(str(spoken).split()).strip(" «»\"'`.,!?").lower()
    for _ in range(len(_TAILS)):
        stripped = text
        for tail in _TAILS:
            if stripped.endswith(f" {tail}"):
                stripped = stripped[: -len(tail) - 1].strip(" ,")
        for head in _HEADS:
            if stripped == head:
                # «Нажми кнопку» без названия — нажимать нечего.
                stripped = ""
            elif stripped.startswith(f"{head} "):
                stripped = stripped[len(head) + 1 :].strip()
        if stripped == text:
            break
        text = stripped
    text = text.strip(" «»\"'`.,!?")
    if not text:
        return []
    variants = [text]
    # Название кнопки склоняют: «нажми кнопку коллекции», а на кнопке
    # «Коллекция». Сравнение идёт началом слова, поэтому достаточно отбросить
    # окончание — тогда одна основа покрывает все падежи. Коротким словам это
    # не нужно и вредно: от «лайк» осталось бы «лай».
    head, _, last = text.rpartition(" ")
    # Только для русского: падежей в английских подписях нет, и «subscribe»
    # незачем превращать в «subscrib».
    if len(last) >= _MIN_STEM and _CYRILLIC.search(last):
        stem = last.rstrip(_ENDINGS)
        if len(stem) >= _MIN_STEM - 1 and stem != last:
            variants.append(f"{head} {stem}".strip())
    latin = romanize(text)
    if latin != text and latin not in variants:
        variants.append(latin)
    return variants


def choose_control(reply: str, controls: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Разобрать ответ модели: номер кнопки из показанного ей списка.

    Ответ числом выбран намеренно. Попроси модель вернуть селектор — получишь
    выдуманный: она честно допишет то, чего на странице нет. Номер же можно
    только либо назвать верно, либо промахнуться по списку, который у нас на
    руках.
    """
    match = _NUMBER.search(str(reply or ""))
    if not match:
        return None
    index = int(match.group())
    if index < 1 or index > len(controls):
        return None
    control = controls[index - 1]
    return dict(control) if isinstance(control, Mapping) else None


def control_plan(control: Mapping[str, Any]) -> list[dict[str, Any]]:
    """План из выбранной кнопки: сначала селектор, потом её подпись.

    Подпись здесь не украшение, а страховка: селектор устареет при ближайшем
    редизайне, а по подписи кнопка найдётся и после него.
    """
    steps: list[dict[str, Any]] = []
    selector = str(control.get("sel") or "").strip()
    if selector:
        steps.append({"click": [selector]})
    name = str(control.get("name") or "").strip().lower()
    if name:
        steps.append({"label": [name]})
    return validate_plan(steps)


def describe(controls: Sequence[Mapping[str, Any]], *, title: str, host: str, want: str) -> str:
    """Собрать вопрос к модели: страница, кнопки, чего мы хотим."""
    lines = [f"Страница: {title} ({host})", "Кнопки:"]
    for number, control in enumerate(controls, start=1):
        lines.append(f"{number}. {str(control.get('name', '')).strip()}")
    lines.append(f"Какую кнопку нажать, чтобы {want}?")
    return "\n".join(lines)


class PageSkill(Skill):
    """Управление содержимым открытой вкладки."""

    meta = SkillMeta(
        name="page",
        description="Управление тем, что открыто во вкладке: плеер, кнопки, лайки",
        version="0.1.0",
    )

    async def on_setup(self) -> None:
        """Прочитать настройки и свои рецепты."""
        self._learn = bool(self.context.setting("learn", True))
        self._task = str(self.context.setting("llm_task", "intent"))
        self._seconds = float(self.context.setting("seek_seconds", 15))
        self._volume_step = float(self.context.setting("volume_step", 0.1))
        self._section = str(self.context.setting("memory_section", "sites"))
        #: Где искать, если открытый сайт искать не умеет. «Включи трек X» — про
        #: музыку, «включи видео X» — про ролики, и путать их нельзя.
        self._music_site = str(self.context.setting("music_site", "яндекс музыка"))
        self._video_site = str(self.context.setting("video_site", "ютуб"))
        #: Выученное читается из памяти один раз и обновляется при записи.
        self._known: dict[str, Any] | None = None
        #: Что нажали последним — это и отменяет «не сохраняй в память».
        #: Не только выученное: общий способ тоже может нажать не ту кнопку, и
        #: тогда отменять нечего, а запомнить «сюда больше не надо» — нужно.
        self._last: dict[str, Any] = {}

        self._own: dict[str, dict[str, list[dict]]] = {}
        for site, actions in dict(self.context.setting("sites", {})).items():
            if not isinstance(actions, Mapping):
                self.log.warning("Рецепты сайта %s пропущены: ожидался список действий", site)
                continue
            cleaned = {
                str(name): validate_plan(plan)
                for name, plan in actions.items()
                if validate_plan(plan)
            }
            if cleaned:
                self._own[str(site).strip().lower()] = cleaned

        self.log.info(
            "Страницы: своих рецептов %d, встроенных %d, обучение %s",
            len(self._own),
            len(SITE_RECIPES),
            "включено" if self._learn else "выключено",
        )

    # --- команды -----------------------------------------------------------

    # Описание нарочно перечисляет живые слова: по нему модель и решает, что
    # «включи видео» — это сюда. Первая строка докстринга уезжает в каталог.
    @tool()
    async def control(self, action: ACTION, site: str = "", seconds: float = 0) -> ToolResult:
        """Включить, поставить на паузу, переключить трек, лайкнуть или перемотать то, что открыто во вкладке браузера.

        :param action: что сделать: play, pause, toggle, next, previous, like,
            mute, unmute, louder, quieter, forward, back.
        :param site: на каком сайте — «ютуб», «яндекс музыка»; пусто — там,
            откуда сейчас идёт звук.
        :param seconds: на сколько секунд перематывать (forward и back).
        """
        return await self._act(str(action), site=site, seconds=seconds)

    @tool(phrases=["нажми {control}", "нажми кнопку {control}", "нажми на {control}",
                   "открой {control} на странице", "открой {control} на сайте",
                   "press {control}", "click {control}"])
    async def press(self, control: str, site: str = "") -> ToolResult:
        """Нажать кнопку на странице по её подписи.

        :param control: что написано на кнопке: «подписаться», «войти».
        :param site: на каком сайте; пусто — в той вкладке, где сейчас работаешь.
        """
        variants = label_variants(control)
        if not variants:
            return ToolResult.failure(
                "не расслышал, что нажимать",
                speech={"ru": "Не понял, что нажать.", "en": "I didn't catch what to press."},
            )
        name = variants[0]
        # Ключ памяти свой на каждую кнопку: «нажми подписаться» на этом сайте
        # выучивается один раз и дальше работает без модели, как и всё
        # остальное.
        return await self._act(
            f"press:{name}",
            site=site,
            extra=[{"label": variants}],
            want=f"нажать кнопку «{name}»",
            speech={
                "ru": (f"Нажал {name}.", f"Готово: {name}.", f"{name} — нажал."),
                "en": (f"Pressed {name}.", f"Done: {name}."),
            },
        )

    # Обёртки под голос: фраза узнаётся мгновенно и бесплатно. В каталог для
    # модели они не идут — она и так всё это умеет через `control`, а каждый
    # инструмент уезжает в неё на каждой неузнанной фразе.
    @tool(routable=False, phrases=["пауза", "поставь на паузу", "останови видео",
                                   "останови музыку", "стоп",
                                   "поставь музыку на паузу", "поставь видео на паузу",
                                   "поставь {site} на паузу", "останови {site}",
                                   "pause", "stop the video", "pause {site}"])
    async def pause(self, site: str = "") -> ToolResult:
        """Поставить воспроизведение на паузу.

        :param site: где именно — «ютуб», «яндекс музыка»; пусто — там, откуда
            идёт звук.
        """
        return await self._act("pause", site=site, soft=True)

    # «Включи видео» и «включи музыку» стоят тут, а не у поиска: когда вкладка
    # уже открыта, это просьба нажать «плей», а не найти что-нибудь новое.
    # Точная фраза побеждает шаблон, поэтому «включи видео {query}» из скилла
    # youtube остаётся рабочим.
    @tool(routable=False, phrases=["сними с паузы", "включи воспроизведение",
                                   "продолжи воспроизведение", "продолжай играть",
                                   "сними музыку с паузы", "сними видео с паузы",
                                   "продолжи музыку", "продолжи видео",
                                   "включи видео", "включи музыку", "включи трек",
                                   "включи песню", "сними {site} с паузы",
                                   "resume", "continue playing"])
    async def play(self, site: str = "") -> ToolResult:
        """Продолжить воспроизведение.

        :param site: где именно; пусто — там, где остановились.
        """
        return await self._act("play", site=site, soft=True)

    @tool(routable=False, phrases=["следующий трек", "следующее видео", "следующая песня",
                                   "переключи трек", "переключи песню", "дальше трек",
                                   "next track", "next video"])
    async def next_track(self) -> ToolResult:
        """Включить следующий трек или видео."""
        return await self._act("next")

    @tool(routable=False, phrases=["предыдущий трек", "предыдущее видео", "прошлый трек",
                                   "верни трек", "previous track", "previous video"])
    async def previous_track(self) -> ToolResult:
        """Вернуться к предыдущему треку или видео."""
        return await self._act("previous")

    @tool(routable=False, phrases=["лайкни", "поставь лайк", "мне нравится",
                                   "лайкни видео", "лайкни трек", "лайкни песню",
                                   "поставь лайк видео", "поставь лайк треку",
                                   "поставь лайк песне",
                                   "like this", "like it"])
    async def like(self) -> ToolResult:
        """Поставить лайк тому, что играет."""
        return await self._act("like")

    @tool(routable=False, phrases=["убери лайк", "сними лайк", "убери лайк с видео",
                                   "убери лайк с трека", "unlike"])
    async def unlike(self) -> ToolResult:
        """Убрать ранее поставленный лайк."""
        return await self._act("unlike")

    @tool(routable=False, phrases=["дизлайк", "поставь дизлайк", "мне не нравится",
                                   "dislike"])
    async def dislike(self) -> ToolResult:
        """Поставить дизлайк."""
        return await self._act("dislike")

    @tool(phrases=["включи трек {track}", "включи песню {track}",
                   "поставь трек {track}", "поставь песню {track}",
                   "включи {track} на сайте", "поставь {track} на сайте",
                   "включи {track} в яндекс музыке",
                   "play the track {track}", "play {track} on the page"])
    async def play_item(self, track: str, site: str = "") -> ToolResult:
        """Включить названное — песню, трек или ролик — там, где сейчас открыт сайт: на Яндекс Музыке это музыка, на YouTube видео; на странице нет — найти в поиске этого же сайта.

        Первая строка уезжает в каталог для модели, и слова в ней подобраны не
        случайно. Сначала там было «из списка на странице», и «включи Don't Stop
        Me Now» модель отправляла на YouTube: искать песню на музыкальном сайте
        по описанию выходило нельзя. Потом обнаружилось следствие похуже: даже с
        открытой Яндекс Музыкой «включи Break My Heart» уводило на ютуб, потому
        что в каталоге был ещё и `youtube.play_video`, а выбор между ними модель
        делала наугад. Правило владельца: **сайт и решает** — на музыкальном
        сайте речь про музыку, на ютубе про видео. Поэтому `play_video` из
        каталога убран, а сказано об этом здесь и прямо.

        Отдельно от «нажми», потому что нажатие по строке трека
        воспроизведение не запускает: оно её только выбирает. Играть начинает
        кнопка внутри строки, и обычно она появляется лишь при наведении.

        :param track: название, как оно написано в списке.
        :param site: на каком сайте; пусто — там, куда смотришь.
        """
        return await self._play(track, site=site, fallback=self._music_site)

    async def _play(self, track: str, *, site: str, fallback: str) -> ToolResult:
        """Включить названное. Отличие трека от ролика — только в том, куда
        отступать, если открытый сайт искать не умеет.
        """
        names = label_variants(track)
        if not names:
            return ToolResult.failure(
                "не расслышал, что включать",
                speech={"ru": "Не понял, что включить.", "en": "I didn't catch what to play."},
            )
        name = names[0]
        speech: dict[str, tuple[str, ...]] = {
            "ru": (f"Включаю {name}.", f"{name}, сейчас.", f"Ставлю {name}.",
                   f"Есть, {name}."),
            "en": (f"Playing {name}.", f"{name}, coming up.", f"Putting on {name}."),
        }
        return await self._act(
            f"item:{name}",
            site=site,
            focused=True,
            extra=[{"item": names, "hint": list(PLAY_HINTS), "play": True}],
            search=name,
            # Открытый сайт искать не умеет (или браузера нет вовсе) — значит
            # идём туда, где это и живёт.
            otherwise=lambda: self._elsewhere(fallback, names, speech),
            speech=speech,
        )

    # Фразы про видео жили в скилле youtube, а он удалён владельцем 30.07.2026
    # («он не нужен»). Забрать их обязательно: «открой видео Мегамозг» иначе
    # снова достаётся шаблону «открой {program}» из запуска программ — а это
    # ровно тот случай, когда голос вызвал запрос прав администратора.
    #
    # В каталог для модели не идёт: «включи X» решает открытый сайт (см.
    # `play_item`), а тут про видео сказано прямо.
    @tool(routable=False,
          phrases=["включи видео {track}", "открой видео {track}",
                   "включи трейлер {track}", "открой трейлер {track}",
                   "включи видео {track} на сайте", "открой видео {track} на сайте",
                   "включи в ютубе {track}", "открой в ютубе {track}",
                   "поставь {track} на ютубе",
                   "play {track} on youtube", "open the video {track}"])
    async def play_video(self, track: str, site: str = "") -> ToolResult:
        """Включить названный ролик: на открытом видеосайте или в его поиске.

        То же, что `play_item`, но отступать некуда иначе: про видео сказано
        прямо, и уводить такую просьбу на музыкальный сайт неверно.

        :param track: название ролика.
        :param site: на каком сайте; пусто — там, куда смотришь.
        """
        return await self._play(track, site=site, fallback=self._video_site)

    @tool(phrases=["введи в поиск {text}", "введи {text} в поиск",
                   "введи в поиск на сайте {text}", "найди на странице {text}",
                   "найди на сайте {text}", "поищи на странице {text}",
                   "поищи на сайте {text}",
                   "type {text}", "search the page for {text}"])
    async def type_in(self, text: str, site: str = "") -> ToolResult:
        """Напечатать текст в поле на странице — обычно в поиск сайта.

        Именно на странице, а не в поисковике: «найди на Яндекс Музыке» и
        «загугли» — разные просьбы, и путать их обидно.

        :param text: что напечатать.
        :param site: на каком сайте; пусто — там, куда смотришь.
        """
        typed = clean_text(text)
        if not typed:
            return ToolResult.failure(
                "не расслышал, что вводить",
                speech={"ru": "Не понял, что ввести.", "en": "I didn't catch what to type."},
            )
        return await self._act(
            "type",
            site=site,
            focused=True,
            extra=[{"type": typed, "submit": True}],
            speech={
                "ru": (f"Ввёл: {typed}.", f"Напечатал: {typed}.", f"Ищу: {typed}."),
                "en": (f"Typed: {typed}.", f"Entered: {typed}.", f"Searching: {typed}."),
            },
        )

    # Направление обязано быть в шаблоне, иначе «пролистай вверх» и «пролистай
    # вниз» — одна и та же фраза с одним и тем же аргументом по умолчанию.
    @tool(phrases=["пролистай {where}", "пролистай страницу {where}",
                   "прокрути {where}", "прокрути страницу {where}",
                   "листай {where}", "в {where} страницы",
                   "scroll {where}", "scroll the page {where}"])
    async def scroll_page(self, where: str = "down", site: str = "") -> ToolResult:
        """Пролистать страницу вверх, вниз, в самое начало или в конец.

        Кнопки для этого нет ни на одном сайте, поэтому и отдельная команда.
        Живой случай: «пролистай страницу вверх» модель разобрала как перемотку
        назад — из всего каталога это было самое близкое.

        :param where: куда: вверх, вниз, в начало, в конец.
        :param site: на каком сайте; пусто — там, куда смотришь.
        """
        way = scroll_way(where)
        if not way:
            return ToolResult.failure(
                f"не понял, куда листать: {where!r}",
                speech={"ru": "Не понял, куда листать.", "en": "I didn't catch which way."},
            )
        return await self._act(
            f"scroll:{way}",
            site=site,
            focused=True,
            extra=[{"scroll": way}],
            speech=SCROLL_SPEECH[way],
        )

    @tool(phrases=["перейди на главную", "перейди на главную страницу",
                   "открой главную страницу", "вернись на главную",
                   "нажми на логотип", "нажми на лого", "нажми логотип",
                   "go to the home page", "go home"])
    async def home(self, site: str = "") -> ToolResult:
        """Перейти на главную страницу сайта, не открывая новую вкладку.

        Отдельная команда, потому что нажать логотип не получается: это ссылка,
        а список для модели собирается из кнопок, и логотипа в нём нет.

        :param site: на каком сайте; пусто — там, куда смотришь.
        """
        if not self.tools.has("browser.page_go"):
            return ToolResult.failure(
                "переходить по адресам умеет расширение браузера, а его нет",
                speech={
                    "ru": "Со страницами я тут не работаю.",
                    "en": "I can't work with pages on this machine.",
                },
            )

        found = await self.tools.invoke("browser.page_target", {"site": site, "active": True})
        if not found.ok or not isinstance(found.value, Mapping):
            return found if not found.ok else self._nothing("home")

        url = str(dict(found.value).get("url", ""))
        home = origin_of(url)
        if not home:
            return ToolResult.failure(
                f"это не обычная страница сайта: {url!r}",
                speech={
                    "ru": "Тут нет главной страницы.",
                    "en": "There's no home page here.",
                },
            )
        if home == url:
            return ToolResult.success(
                {"url": url},
                speech={"ru": "Мы и так на главной.", "en": "We're already on the home page."},
            )

        moved = await self.tools.invoke(
            "browser.page_go", {"url": home, "tab": int(dict(found.value).get("tabId") or 0)}
        )
        if not moved.ok:
            return moved
        self.log.info("Перешёл на главную: %s", home)
        return ToolResult.success(
            {"url": home},
            speech={"ru": "Открыл главную страницу.", "en": "Home page it is."},
        )

    @tool(routable=False, phrases=["включи первое видео", "открой первое видео",
                                   "включи первый результат", "открой первую ссылку",
                                   "включи первое", "play the first video"])
    async def open_first(self, site: str = "") -> ToolResult:
        """Открыть первый результат на странице выдачи.

        :param site: где именно; пусто — в той вкладке, куда сейчас смотришь.
        """
        # Именно вкладка в фокусе: команда идёт следом за поиском, а звук в это
        # время может идти из соседнего окна — туда нажимать нельзя.
        return await self._act("first", site=site, focused=True)

    @tool(routable=False, phrases=["выключи звук во вкладке", "заглуши вкладку",
                                   "mute the tab"])
    async def mute_tab(self) -> ToolResult:
        """Выключить звук во вкладке, не трогая громкость системы."""
        return await self._act("mute")

    @tool(routable=False, phrases=["включи звук во вкладке", "верни звук во вкладке",
                                   "unmute the tab"])
    async def unmute_tab(self) -> ToolResult:
        """Вернуть звук во вкладке."""
        return await self._act("unmute")

    @tool(routable=False, phrases=["перемотай вперёд", "перемотай вперёд {seconds}",
                                   "перемотай на {seconds}", "промотай вперёд {seconds}",
                                   "skip ahead", "skip ahead {seconds}"])
    async def forward(self, seconds: float = 0) -> ToolResult:
        """Перемотать вперёд.

        :param seconds: на сколько секунд; пусто — на обычный шаг из конфига.
        """
        return await self._act("forward", seconds=seconds)

    @tool(routable=False, phrases=["перемотай назад", "перемотай назад {seconds}",
                                   "отмотай назад {seconds}", "верни назад {seconds}",
                                   "skip back", "skip back {seconds}"])
    async def back(self, seconds: float = 0) -> ToolResult:
        """Перемотать назад.

        :param seconds: на сколько секунд; пусто — на обычный шаг из конфига.
        """
        return await self._act("back", seconds=seconds)

    # --- работа ------------------------------------------------------------

    async def _act(
        self,
        action: str,
        *,
        site: str = "",
        seconds: float = 0,
        extra: Sequence[Mapping[str, Any]] = (),
        want: str = "",
        speech: Mapping[str, Sequence[str]] | None = None,
        soft: bool = False,
        focused: bool = False,
        search: str = "",
        open_missing: bool = False,
        otherwise: Callable[[], Awaitable[ToolResult]] | None = None,
    ) -> ToolResult:
        """Выполнить действие в подходящей вкладке.

        Порядок такой: найти вкладку → собрать план из известного → выполнить →
        и только при неудаче один раз спросить модель и запомнить ответ.

        :param search: что искать на самом сайте, если на странице этого не
            нашлось. «Включи Don't Stop Me Now» — обычно просьба найти трек, а
            не только нажать на него: на открытой странице его нет, потому что
            никто его туда не выводил.

        :param soft: считать название сайта пожеланием, а не требованием.
            «Поставь ютуб на паузу» при закрытом ютубе разумно понять как
            «поставь на паузу»: остановить просят то, что звучит, и молчать
            из-за неудачно названного сайта тут хуже, чем выполнить. Для
            `press` и явного `control` название остаётся требованием: нажать
            кнопку не на том сайте — это уже не мелочь.
        :param focused: работать с вкладкой в фокусе, даже если звук идёт из
            другой. Для команд про содержимое («включи первое видео») это
            единственно верно: смотрят в одну вкладку, а играет другая.
        :param otherwise: что делать, если на странице не нашлось **ничего**.
            Нужно ровно одному случаю: «включи X» с сайтом, где искать нечем
            (или вообще без открытого браузера) — это уже не про страницу, и
            выполняет такую просьбу ютуб. Тишина после нажатия сюда не входит:
            там нашлось и нажалось, и уводить владельца на другой сайт неверно.
        """
        if not self.tools.has("browser.page_run"):
            if otherwise is not None:
                return await otherwise()
            return ToolResult.failure(
                "работать со страницей умеет расширение браузера, а скилл browser не подключён",
                speech={
                    "ru": "Со страницами я тут не работаю.",
                    "en": "I can't work with pages on this machine.",
                },
            )

        # Вкладку узнаём заранее: без неё неизвестен сайт, а значит и рецепт.
        # Это один обмен по локальному сокету, зато дальше всё однозначно.
        where = {"site": site, "active": focused, "open_missing": open_missing}
        found = await self.tools.invoke("browser.page_target", where)
        if not found.ok and site and soft:
            self.log.info("Вкладки %r не нашлось — работаю с той, что звучит", site)
            found = await self.tools.invoke("browser.page_target", {"active": focused})
        if not found.ok or not isinstance(found.value, Mapping):
            if otherwise is not None:
                # Подходящей вкладки нет вовсе — значит речь и не про страницу.
                return await otherwise()
            return found if not found.ok else self._nothing(action)
        target = dict(found.value)
        tab = int(target.get("tabId") or 0)
        host = host_of(str(target.get("url", "")))

        steps = self._plan(action, host, seconds=seconds, extra=extra)

        # На сайте, где искать нечем, и на странице делать нечего.
        #
        # Живой случай, стоивший двух заходов: открыта была веб-панель, а в ней
        # висел текст прошлого разговора — вместе со ссылкой
        # «youtube.com/results?search_query=dua+lipa+-+break+my+heart». Слова
        # просьбы в ней нашлись все, ссылка «совпала», Jarvis её нажал и честно
        # доложил «нашёл, но не заиграло». Музыкальный сайт при этом стоял
        # рядом открытым и не получил ни одной команды.
        #
        # Признак прямой: `search` заполняют только просьбы «включи названное», а
        # `search_url_for` знает, у каких сайтов есть своя выдача. Нет её — сайт
        # не про музыку и не про видео, и сравнивать названия на нём незачем.
        if search and otherwise is not None and not search_url_for(host, search):
            self.log.info("На %s искать нечем — на странице даже не смотрю", host or "этой странице")
            return await otherwise()

        result = await self._run(steps, tab)
        if search and (result is None or silent(result)):
            # Нашлось не то или заиграть не смогло — поищем на самом сайте.
            better = await self._through_search(search, steps, tab=tab, host=host)
            if better is not None:
                result = better
        if result is not None:
            self._note_attempt(host, action, result, learned=False)
            if silent(result):
                return self._not_playing(action, result)
            return self._done(action, result, speech)

        learned = await self._learn_action(
            action, tab=tab, host=host, title=str(target.get("title", "")), want=want
        )
        if learned is not None:
            self._note_attempt(host, action, learned, learned=True)
            return self._done(action, learned, speech)
        if otherwise is not None:
            return await otherwise()
        return self._nothing(action, host=host)

    def _note_attempt(
        self, host: str, action: str, result: Mapping[str, Any], *, learned: bool
    ) -> None:
        """Запомнить, что именно нажали, — на случай «это не то».

        Медиа-шаги сюда не попадают: пауза есть пауза, отменять в ней нечего.
        """
        if result.get("done") not in ("label", "click"):
            self._last = {}
            return
        self._last = {
            "host": host,
            "action": action,
            "learned": learned,
            "name": str(result.get("detail", "")).strip(),
            "sel": str(result.get("sel", "")).strip(),
        }

    def _plan(
        self,
        action: str,
        host: str,
        *,
        seconds: float = 0,
        extra: Sequence[Mapping[str, Any]] = (),
    ) -> list[dict[str, Any]]:
        """Собрать варианты для действия на этом сайте.

        Порядок источников — от самого частного к самому общему: свой рецепт
        из конфига, выученное для этого сайта, встроенный рецепт, общий способ.
        Написанное человеком идёт первым: если он что-то поправил руками, это
        и есть ответ, а выученное когда-то могло уже устареть.
        """
        own = recipes_for(host, self._own).get(action, ())
        known = recipes_for(host, self._sites_memory()).get(action, ())
        built_in = recipes_for(host, SITE_RECIPES).get(action, ())
        common = ACTIONS.get(action, ())
        merged = merge_plans(
            validate_plan(own), validate_plan(known), built_in, validate_plan(extra), common
        )
        # То, что уже пробовали и что оказалось не тем, из плана вычитается:
        # второй раз нажимать ту же не ту кнопку незачем.
        merged = without_rejected(merged, self._rejected(host, action))
        return with_amount(
            merged,
            seconds=seconds if seconds > 0 else self._seconds,
            step=self._volume_step,
        )

    async def _run(self, plan: Sequence[Mapping[str, Any]], tab: int) -> dict[str, Any] | None:
        """Отправить план в страницу; ``None`` — ни один вариант не сработал."""
        steps = validate_plan(plan)
        if not steps:
            return None
        result = await self.tools.invoke(
            "browser.page_run", {"plan": steps, "tab": tab}
        )
        # Сырой обмен — в файл, всегда и без условий. Это и есть ответ на вопрос
        # «что вообще произошло»: план, который ушёл, и ответ, который пришёл.
        # Условных подробностей тут больше нет — на них уже наступали.
        self.log.debug("План %s → ответ %s", steps, result.value if result.ok else result.error)
        if not result.ok or not isinstance(result.value, Mapping):
            return None
        if result.value.get("done"):
            return dict(result.value)

        # Название не нашлось. Что было на странице — единственный способ
        # понять, дело в услышанном названии или страница ещё не дорисовалась.
        #
        # Считанные элементы важнее списка, и вот почему. Пустой список в лог не
        # попадал вовсе — условие было «если есть что показать». А на живой
        # выдаче Яндекс Музыки четыре попытки подряд отдали ровно пустоту, и в
        # логе от них не осталось ни строки: выглядело как «Jarvis молча
        # передумал». Ноль элементов и сорок элементов без совпадения — разные
        # болезни: первая лечится ожиданием, вторая названием.
        saw = result.value.get("saw")
        if saw is None:
            return None
        counted = int(result.value.get("counted") or 0)
        where = str(result.value.get("url", "")) or "страница"
        if not counted:
            self.log.info("На %s пока ни одного элемента — страница не отрисовалась", where)
        else:
            self.log.info(
                "На странице видно (всего %d): %s",
                counted,
                "; ".join(str(item) for item in saw),
            )
        return None

    async def _elsewhere(
        self, site: str, names: Sequence[str], speech: Mapping[str, Sequence[str]]
    ) -> ToolResult:
        """Искать на своём сайте: на открытом искать оказалось нечем.

        Сюда попадают случаи, где текущий сайт не умеет искать — или браузер
        вообще закрыт. Сначала тут был жёстко ютуб, и это была ошибка: «включи
        **трек** Dua Lipa» с открытым посторонним сайтом включало клип, хотя
        слово «трек» сказано прямо и Яндекс Музыка была открыта рядом.

        Куда идти, решает вызывающий: `music_site` для трека, `video_site` для
        ролика. Сайт сначала открывается (без его вкладки искать негде), а дальше
        всё как обычно: не нашлось на странице — идём в поиск сайта.
        """
        name = names[0]
        if not site:
            return self._nothing(f"item:{name}")

        self.log.info("На открытом сайте искать нечем — ищу %r на %s", name, site)
        # Вкладку сайта открывает `page_target` — **в фоне**, если её нет.
        # Показывать музыкальный сайт незачем: владелец просил не выдёргивать его
        # из того, чем он занят, и дело можно сделать молча.
        #
        # Второй попытки не будет: `otherwise` тут не передаём, иначе получился
        # бы круг из «искать негде» в самого себя.
        return await self._act(
            f"item:{name}",
            site=site,
            open_missing=True,
            extra=[{"item": list(names), "hint": list(PLAY_HINTS), "play": True}],
            search=name,
            speech=speech,
        )

    async def _through_search(
        self, query: str, steps: Sequence[Mapping[str, Any]], *, tab: int, host: str
    ) -> dict[str, Any] | None:
        """Поискать названное на самом сайте и попробовать снова.

        «Включи Don't Stop Me Now» — это «найди и включи»: на открытой странице
        трека нет и быть не должно. Выдача открывается **в этой же вкладке** и
        ссылкой, а не набором текста в строке поиска: результат тот же, а
        ошибиться нечем — тот же довод, что и у `browser.search`.

        Ждать приходится дважды. Расширение ждёт события загрузки, но выдачу
        такие сайты дорисовывают скриптом уже после него: разметка есть, треков
        ещё нет. Поэтому план прогоняется по кругу несколько раз.
        """
        url = search_url_for(host, query)
        if not url:
            return None
        if not self.tools.has("browser.page_go"):
            return None

        moved = await self.tools.invoke("browser.page_go", {"url": url, "tab": tab})
        if not moved.ok:
            self.log.info("Поиск на %s не открылся: %s", host, moved.error)
            return None
        self.log.info("На странице %r не нашлось — открыл поиск сайта: %s", query, url)

        # На **своей** выдаче верхний результат главнее сравнения слов.
        #
        # Порядок в выдаче расставил сайт — по всему, что он знает о треках. У
        # нас же есть только услышанное название, уже искажённое распознаванием,
        # и ранжировать им поверх готового порядка — значит менять хорошее знание
        # на плохое. Живой случай: «Dua Lipa Break My Heart» пришло как «2, липа
        # Brake My Heart», верхней карточкой Яндекс поставил трек Dua Lipa, а
        # совпадение по словам увело в ремиксы посторонних артистов ниже — «break»
        # и «heart» нашлись и там.
        #
        # Только здесь и только так: на странице, которую открыл сам владелец,
        # никакого «верхнего результата» нет, и там название решает всё.
        plan = self._with_top(host, steps)

        # Повторяем только пока **не нашли**: выдача дорисовывается скриптом уже
        # после события загрузки, и первые попытки честно натыкаются на пустоту.
        # А вот «нашли, но тишина» повторять нечего: строка на месте, все способы
        # включить её уже перебраны внутри страницы, и второй круг — это ещё
        # столько же нажатий впустую. Живой случай: четыре круга по два с
        # лишним секунды складывались в 18 с на одну команду.
        for attempt in range(SEARCH_ATTEMPTS):
            if attempt:
                await asyncio.sleep(SEARCH_DELAY)
            result = await self._run(plan, tab)
            if result is not None:
                return result
        return None

    def _with_top(
        self, host: str, steps: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Дописать в шаг `item` «сначала верхний результат выдачи».

        Селекторы берутся из рецепта `first` этого сайта — того же, которым
        работает «включи первый результат». Один источник на две команды: сайт
        переделает выдачу — правится одно место.
        """
        top = [
            selector
            for step in self._plan("first", host)
            for selector in step.get("click", ())
        ]
        if not top:
            return list(steps)
        return validate_plan(
            [{**step, "prefer": top} if "item" in step else step for step in steps]
        )

    async def _learn_action(
        self, action: str, *, tab: int, host: str, title: str, want: str
    ) -> dict[str, Any] | None:
        """Спросить у модели, что нажать, и запомнить ответ навсегда.

        Это единственное место скилла, где тратятся токены, и оно устроено так,
        чтобы срабатывать один раз на сайт и действие: удачный выбор уходит в
        память и дальше берётся оттуда.
        """
        if not self._learn or not host:
            return None
        if not learnable(action):
            # Спрашивать бессмысленно: нужного в списке кнопок нет.
            self.log.info(
                "Для %s модель не поможет — в списке только кнопки, а нужно другое", action
            )
            return None
        if not self.context.llm.available:
            self.log.debug("Модель не настроена — учиться не у кого")
            return None

        probed = await self.tools.invoke("browser.page_probe", {"tab": tab})
        controls = list((probed.value or {}).get("controls", [])) if probed.ok else []
        # Отвергнутые кнопки модели даже не показываем: выбрать то, что уже
        # признано неверным, она не должна — и объяснять ей это не нужно.
        rejected = self._rejected(host, action)
        if rejected:
            names = {str(item.get("name", "")).strip().lower() for item in rejected}
            selectors = {str(item.get("sel", "")).strip() for item in rejected}
            controls = [
                item
                for item in controls
                if str(item.get("name", "")).strip().lower() not in names
                and str(item.get("sel", "")).strip() not in selectors
            ]
            self.log.info(
                "Для %s на %s уже отвергнуто кнопок: %d — предлагаю модели остальные",
                action,
                host,
                len(rejected),
            )
        if not controls:
            return None

        question = describe(
            controls, title=title, host=host, want=want or INTENT_TEXT.get(action, action)
        )
        try:
            reply = await self.context.llm.ask(question, task=self._task, system=_LEARN_SYSTEM)
        except Exception as exc:  # noqa: BLE001 — без модели команда просто не выйдет
            self.log.warning("Не удалось спросить модель про кнопку: %s", exc)
            return None

        control = choose_control(reply, controls)
        if control is None:
            self.log.info("Модель не нашла кнопку для %s на %s (ответ %r)", action, host, reply)
            return None

        # Название кнопки назвал сам владелец — значит выбор модели можно
        # проверить, и проверить нужно: иначе «нажми скопировать» превращается
        # в «Ещё» и запоминается таким навсегда.
        if action.startswith("press:"):
            asked = action.split(":", 1)[1]
            if not resembles(str(control.get("name", "")), asked):
                self.log.info(
                    "Модель предложила %r вместо %r — на просьбу это не похоже, отказываюсь",
                    control.get("name", ""),
                    asked,
                )
                return None

        plan = control_plan(control)
        result = await self._run(plan, tab)
        if result is None:
            return None

        await self._remember(host, action, plan)
        self.log.info(
            "Запомнил: %s на %s — кнопка %r", action, host, control.get("name", "")
        )
        return result

    # --- память ------------------------------------------------------------

    def _sites_memory(self) -> dict[str, Any]:
        """Выученные рецепты по сайтам."""
        return {
            host: value.get("actions", {})
            for host, value in (self._known or {}).items()
            if isinstance(value, Mapping)
        }

    def _rejected(self, host: str, action: str) -> list[dict[str, Any]]:
        """Кнопки, которые для этого действия уже признаны не теми."""
        rejected = {
            name: value.get("rejected", {})
            for name, value in (self._known or {}).items()
            if isinstance(value, Mapping)
        }
        listed = recipes_for(host, rejected).get(action, ())
        return [dict(item) for item in listed if isinstance(item, Mapping)]

    async def _write(self, host: str, entry: Mapping[str, Any]) -> bool:
        """Сохранить запись о сайте целиком и обновить кеш."""
        try:
            await self.context.memory.documents.set(self._section, host, dict(entry))
        except Exception as exc:  # noqa: BLE001 — не записалось, но команда выполнена
            self.log.warning("Не смог записать память сайта %s: %s", host, exc)
            return False
        cache = dict(self._known or {})
        cache[host] = dict(entry)
        self._known = cache
        return True

    async def _entry(self, host: str) -> dict[str, Any]:
        """Прочитать запись о сайте: сперва из кеша, иначе из памяти."""
        cached = (self._known or {}).get(host)
        if isinstance(cached, Mapping):
            return dict(cached)
        try:
            return dict(await self.context.memory.documents.get(self._section, host, {}) or {})
        except Exception as exc:  # noqa: BLE001 — память необязательна
            self.log.warning("Не смог прочитать память сайта %s: %s", host, exc)
            return {}

    async def _remember(self, host: str, action: str, plan: Sequence[Mapping[str, Any]]) -> None:
        """Записать удачный способ в память — раздел ``sites``."""
        entry = await self._entry(host)
        actions = dict(entry.get("actions", {}))
        actions[action] = [dict(step) for step in plan]
        entry["actions"] = actions
        await self._write(host, entry)

    @tool(routable=False)
    async def forget_last(self) -> ToolResult:
        """Забыть последнее нажатие: не только выученное, но и **куда** нажали.

        Отмена работает в две стороны. Выученный способ убирается — раз он не
        тот, второй раз его брать незачем. И отдельно запоминается сама кнопка:
        «для этого действия на этом сайте — не сюда». В следующий раз она
        выпадет из плана, а если дело дойдёт до модели, ей эту кнопку даже не
        покажут. Так «попробуй другую» получается само, без второй просьбы.

        Работает и там, где ничего не выучивалось: нажать не то мог и общий
        способ по подписи кнопки.

        Имя не случайное: по нему ядро находит все скиллы, которые учатся сами,
        и общая команда «не сохраняй в память» отменяет и их работу тоже.
        """
        last = dict(self._last)
        host = str(last.get("host", ""))
        action = str(last.get("action", ""))
        if not host or not action:
            return ToolResult.success("")

        entry = await self._entry(host)
        changed = False

        if last.get("learned"):
            actions = dict(entry.get("actions", {}))
            if actions.pop(action, None) is not None:
                entry["actions"] = actions
                changed = True

        name = str(last.get("name", ""))
        selector = str(last.get("sel", ""))
        if name or selector:
            rejected = dict(entry.get("rejected", {}))
            listed = [
                dict(item)
                for item in rejected.get(action, ())
                if isinstance(item, Mapping)
            ]
            mark = {"name": name, "sel": selector}
            if mark not in listed:
                listed.append(mark)
                # Помним последние: если не подошли пять кнопок, дело не в них.
                rejected[action] = listed[-MAX_REJECTED:]
                entry["rejected"] = rejected
                changed = True

        if not changed or not await self._write(host, entry):
            return ToolResult.success("")

        self._last = {}
        self.log.info(
            "Больше не нажимаю %r для %s на %s", name or selector, action, host
        )
        described = f"{action} на {host}"
        return ToolResult.success(f"{described}: {name}" if name else described)

    async def on_start(self) -> None:
        """Поднять из памяти то, что уже выучено."""
        try:
            self._known = dict(await self.context.memory.documents.read(self._section))
        except Exception as exc:  # noqa: BLE001 — память необязательна для команд
            self.log.warning(
                "Раздел памяти %r недоступен (%s) — выученные способы не подхватятся. "
                "Проверь, что он объявлен в memory.documents",
                self._section,
                exc,
            )
            self._known = {}
        if self._known:
            self.log.info("Выученных сайтов в памяти: %d", len(self._known))

    # --- ответы ------------------------------------------------------------

    def _done(
        self, action: str, result: Mapping[str, Any], speech: Mapping[str, Sequence[str]] | None
    ) -> ToolResult:
        """Успех: сказать коротко, подробности оставить в значении."""
        lines = speech or SPEECH.get(action, _ANYWAY)
        detail = str(result.get("detail", "")).strip()
        self.log.info(
            "Страница %s: %s (%s)", result.get("url", ""), action, detail or result.get("done")
        )
        self._log_nearby(result)
        return ToolResult.success(dict(result), speech={key: tuple(items) for key, items in lines.items()})

    def _not_playing(self, action: str, result: Mapping[str, Any]) -> ToolResult:
        """Строку нашли и нажали, а звука нет.

        Отчитаться «включаю» в тишину нельзя — это ровно та ложь, за которую
        поправлен свободный разговор. Живой случай: на Яндекс Музыке трек
        нашёлся, кнопка «Воспроизведение» нажалась, ассистент сказал «включаю»,
        и ничего не заиграло.

        Вслух называется **то, что просили**, а не подпись найденного. Подпись
        бывает какой угодно: однажды в неё уехала склеенная выдача целиком, и
        ассистент честно прочитал вслух «new rulesdua lipa03:29let you break my
        heart againmorlix…». В логе подпись остаётся — там она и нужна.
        """
        detail = str(result.get("detail", "")).strip()
        asked = action.split(":", 1)[1] if ":" in action else action
        self.log.info("Нажал %r, но звука так и нет: %s", detail, result.get("url", ""))
        self._log_nearby(result)
        return ToolResult.failure(
            f"нашёл {detail!r}, но воспроизведение не началось",
            speech={
                "ru": (
                    f"Нашёл {asked}, но включить не получилось.",
                    f"{asked} нашёл, а включить не смог.",
                ),
                "en": (
                    f"I found {asked}, but it wouldn't start playing.",
                    f"Found {asked}, couldn't start it.",
                ),
            },
        )

    def _log_nearby(self, result: Mapping[str, Any]) -> None:
        """Написать в лог, что было рядом со строкой.

        Расширение присылает этот список, когда кнопку воспроизведения найти не
        удалось или звук так и не появился. По нему рецепт сайта дописывается
        точно, а не угадывается по чужой разметке вслепую.
        """
        nearby = result.get("buttons")
        if not nearby:
            return
        listed = "; ".join(
            f"{str(item.get('name', '')) or '(без подписи)'} [{item.get('sel', '')}]"
            for item in nearby
            if isinstance(item, Mapping)
        )
        self.log.info("Кнопки рядом со строкой: %s", listed)

    @staticmethod
    def _nothing(action: str, *, host: str = "") -> ToolResult:
        """Отказ, когда ни один способ не подошёл."""
        where = f" на {host}" if host else ""
        return ToolResult.failure(
            f"не нашёл, чем выполнить {action}{where}",
            speech={
                "ru": "Не нашёл, чем это сделать на странице.",
                "en": "I couldn't find a control for that on the page.",
            },
        )

    async def health(self) -> HealthStatus:
        """Готовность: есть ли мост к странице и что уже выучено."""
        if not self.tools.has("browser.page_run"):
            return HealthStatus.degraded("скилл browser не подключён — страницы недоступны")
        learned = sum(len(actions) for actions in self._sites_memory().values())
        return HealthStatus.healthy(
            f"рецептов своих {len(self._own)}, встроенных {len(SITE_RECIPES)}, "
            f"выученных {learned}"
        )
