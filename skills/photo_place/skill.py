"""Где снято: место по фотографии — на экране или в файле.

Скилл появился из живого разбора 12.09.2026 и за день переделывался трижды.
Каждая переделка — отдельное возражение владельца, и порядок их важен.

**«EXIF не всегда есть».** Первая версия читала координаты из файла. Проверка
подтвердила возражение буквально: из сорока снимков на машине владельца GPS не
оказалось **ни у одного**. Мессенджеры вырезают его при отправке, у скриншота
его нет по построению, а именно их ассистенту и показывают. EXIF остался, но
как удача, а не опора: когда он есть, он точен.

**«Город я и сам найду».** Вторая версия просила у модели до трёх версий, каждую
точкой, и выбирала ту, что нашлась на карте точнее. Живой прогон показал, что
это **худшее из возможных решений**: по фотографии дороги под Анталией модель
выдала «перекрёсток D400 и улицы 2500. Sk» с координатами, промахнулась на
двенадцать километров — и **не назвала город**, хотя сама же прочитала на
вывеске «ANTALYA BÜYÜKŞEHİR BELEDİYESİ» и номер машины на 07. Требование «дай
точку» не делает модель точнее, оно заставляет её сочинять.

**Отсюда нынешнее устройство — лестница.** Модель перечисляет зацепки, а потом
заполняет ступени от страны к месту, и **на каждой имеет право написать «нет»**.
Проверяются они от частного к общему, и берётся первая, которую знает геокодер:
порядок ступеней — это порядок доверия самой модели, спорить с ним нечем.
Ступень МЕСТО дополнительно просеивается от выдуманных адресов.

**Точность измеряется, а не обещается**, и говорится вслух: «с точностью до
здания», «только до города». У протяжённого объекта её даёт рамка, у точечного —
ранг геокодера; рамка у метки всегда одиннадцать метров, что у отеля, что у
Средиземного моря. На том же снимке ответ стал «Анталья, только до города» с
обещанной точностью в двадцать пять километров и настоящим промахом в десять:
общо, зато честно и в пределах обещанного.

**Версия проверяется спутником, и это единственное, что превращает догадку в
ответ.** Выбранное место показывается зрячей модели рядом с фотографией: сходится
ли план — дороги, крыши, граница зелени. Не сошлось — версия отбрасывается, и
берётся следующая ступень, более общая. Замер на Дмитровском кремле: десять
баллов верному месту и ноль трём чужим, разделение полное.

Две оговорки, обе из замеров. **Спутник отстаёт от карты на годы**: под Анталией
съёмка оказалась старше самой дороги со снимка, там теплицы и просёлок — сверять
было не с чем, и модель честно поставила ноль. Поэтому молчание спутника версию
не отвергает: «не проверили» и «не похоже» — разные вещи. И **общие версии не
сверяются вовсе**: у города на снимке сверху нет той геометрии, что видна на
фотографии.

**Догадку и замер не путаем.** Координаты из файла — «снято здесь», узнавание по
виду — «похоже на», сверенная версия — «сверил со спутником, сходится».
"""

from __future__ import annotations

import asyncio
import base64
import io
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from jarvis.core.contracts import ToolResult
from jarvis.core.errors import LLMNotConfigured
from jarvis.core.llm import Message
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool

#: Профиль зрячей модели. Тот же, что у скилла `screen`: модель обязана уметь
#: картинки, а разбор команд идёт на самой дешёвой.
VISION_TASK = "vision"

#: Геокодер OpenStreetMap: без ключа, по названию отдаёт точку и рамку объекта.
NOMINATIM = "https://nominatim.openstreetmap.org"

#: Представляться геокодеру обязательно: их правила требуют узнаваемого имени,
#: анонимные запросы блокируют.
USER_AGENT = "Jarvis voice assistant (github.com/mcdima0001/Jarvis)"

#: Сколько ждать геокодер. Ответ нужен внутри голосовой команды.
GEOCODE_TIMEOUT = 8.0

#: Сколько версий проверять. Каждая стоит запроса к чужому сервису, а правило
#: Nominatim — не чаще раза в секунду, то есть версии ещё и растягивают ответ.
MAX_CANDIDATES = 5

#: Запас за границей найденной области, метров. Снимок с окраины города вполне
#: сделан за его чертой, и отбрасывать такую версию было бы неверно.
MARGIN = 3_000.0

#: Длинная сторона картинки для модели. Тот же предел, что у зрения.
LIMIT = 1920

#: Что читаем как фотографию. EXIF бывает только в части форматов, но зрение
#: работает с любым, поэтому список шире.
PICTURES = frozenset({".jpg", ".jpeg", ".jpe", ".png", ".webp", ".tif", ".tiff", ".bmp"})

_ASK = {
    "ru": """Определи, где снята эта фотография. Отвечай как человек, который ищет
место всерьёз, а не с первого взгляда.

Сначала перечисли зацепки: язык и текст на вывесках, стиль архитектуры, рельеф и
силуэт гор, растительность, дорожная разметка и знаки, номера машин, тип столбов
и ограждений, положение солнца.


Часть зацепок называет место прямо, а не намёком: код на автомобильном номере,
вывеска местного органа власти, телефонный код, название на дорожном указателе.
Если такая зацепка есть — выведи из неё область и город, это не догадка.

Потом ответь по ступеням, от общего к частному. Заполняй только те ступени, в
которых **уверен**; на остальных пиши слово нет.

ЗАЦЕПКИ: <через запятую>
СТРАНА: <страна или нет>
ГОРОД: <город или нет>
РАЙОН: <район, посёлок или нет>
МЕСТО: <конкретное узнаваемое место: здание, отель, пляж, достопримечательность
        — ТОЛЬКО если правда его узнаёшь, иначе нет. Назови его так, как оно
        подписано на карте: коротким общеизвестным названием, а не полным
        официальным. «Дмитровский кремль», а не «Успенский собор Дмитровского
        кремля»: длинное официальное название карта чаще всего не знает>
МЕСТНОЕ: <название ступени МЕСТО на местном языке или по-английски, иначе нет>

**Выдуманная улица или перекрёсток хуже честного города.** Не называй адрес,
номер дороги или пересечение улиц, если не узнаёшь место по виду: точный на вид
ответ, взятый наугад, вреднее общего, но верного.""",
    "en": """Work out where this photo was taken. Answer like a person who looks
into it properly, not at first glance.

First list the clues: language and text on signs, architecture, terrain and
mountain silhouette, vegetation, road markings and signs, number plates, poles
and railings, the position of the sun.


Some clues name the place outright rather than hint at it: the code on a number
plate, a local government sign, a phone code, a name on a road sign. If you have
such a clue, derive the region and city from it — that is not guesswork.

Then answer in steps, from general to specific. Fill in only the steps you are
**sure** about; write no on the others.

CLUES: <comma separated>
COUNTRY: <country or no>
CITY: <city or no>
DISTRICT: <district, suburb or no>
PLACE: <a specific recognisable place: building, hotel, beach, landmark — ONLY
        if you truly recognise it, otherwise no. Name it the way a map labels
        it: the short common name, not the full official one. A map usually
        does not know long official titles>
LOCAL: <the PLACE name in the local language, otherwise no>

**An invented street or crossroads is worse than an honest city.** Do not give an
address, road number or street intersection unless you recognise the place by
sight: a precise-looking guess is more harmful than a general but correct one.""",
}

#: Подпись строки с зацепками. Обе раскладки: модель отвечает на языке вопроса.
_CLUES = ("зацепки:", "clues:")

#: Подписи ступеней: как их зовут по-русски и по-английски.
_FIELDS = {
    "country": ("страна:", "country:"),
    "city": ("город:", "city:"),
    "district": ("район:", "district:"),
    "place": ("место:", "place:"),
    "local": ("местное:", "local:"),
}

#: Признаки выдуманного места. Модель, которую заставляют назвать точку, не
#: отказывается — она сочиняет адрес, и звучит он убедительно. В живом прогоне
#: 12.09.2026 по фотографии дороги под Анталией она выдала «перекрёсток D400 и
#: улицы 2500. Sk» с координатами, промахнувшись на двенадцать километров, и при
#: этом **не назвала город**, хотя сама же прочитала на вывеске «ANTALYA
#: BÜYÜKŞEHİR BELEDİYESİ» и номер машины на 07.
#:
#: Поэтому такие ответы отбрасываются на ступени МЕСТО: перекрёсток, номер
#: дороги, сокращение улицы. Достопримечательность так не называют, а выдумка
#: выглядит именно так.
_FABRICATED = re.compile(
    r"перекрёст|перекрест|пересечени|intersection|junction|"
    r"улиц|sokak|sk\.|cd\.|blv|caddesi|"
    r"[deo]\s?\d{3}|шоссе|highway",
    re.IGNORECASE,
)

#: Координаты в свободном виде: «36.8969, 30.7133». Знак и дробная часть
#: необязательны, разделитель — запятая или точка с запятой.
_POINT = re.compile(r"^\s*(-?\d{1,3}(?:[.,]\d+)?)\s*[;,]\s*(-?\d{1,3}(?:[.,]\d+)?)\s*$")

#: Что срезать с краёв названия: обрамление и знаки, но не буквы.
_EDGES = " \t«»\"'`.,:;!?"

#: Чем модель отказывается от ответа целиком. Проверяется началом строки.
_REFUSALS = ("не знаю", "не могу", "непонятно", "unknown", "i cannot", "i can't", "unable")

#: Чем модель отказывается от **одной ступени**. Сравнивается целиком, а не
#: началом, и это важно: «no» началом совпало бы с Новосибирском, а «нет» — с
#: Нетанией. Пустая ступень значит «не знаю», и выдумывать за модель нечего.
_EMPTY = frozenset({"нет", "не", "no", "none", "n/a", "-", "—", "неизвестно", "unknown"})


def is_empty(value: str) -> bool:
    """Пустая ли ступень лестницы."""
    return value.strip(_EDGES).lower() in _EMPTY

#: Насколько точен найденный объект, метров по большей стороне, и как это назвать
#: вслух. Пороги из замера по геокодеру 12.09.2026: здание — 47 м, башня — 175,
#: площадь — 359, город — от двадцати километров.
PRECISION = (
    (250.0, "с точностью до здания", "within about a hundred metres"),
    (2_000.0, "с точностью до квартала", "within a couple of blocks"),
    (100_000.0, "только до города", "the city only"),
    (float("inf"), "только до региона", "the region only"),
)

#: Насколько точен объект по рангу геокодера: ранг → метры. Ранг — это
#: подробность объекта в шкале OSM, от страны (4) до дома (30).
#:
#: **Рамка объекта тут не годится, и это выяснилось замером.** У точечных
#: объектов геокодер отдаёт рамку в одиннадцать метров — всегда, чем бы объект
#: ни был: и у отеля, и у семикилометрового пляжа, и у Средиземного моря. То
#: есть по рамке метка неотличима от здания. Ранг же честен: у моря он 2, у
#: города 16, у здания 30.
RANK_METRES = (
    (30, 100.0),
    (27, 300.0),
    (24, 1_000.0),
    (20, 3_000.0),
    (16, 25_000.0),
    (12, 60_000.0),
    (0, 500_000.0),
)

#: Спутниковые тайлы для сверки. Источник открытый, просит только честно
#: представиться и не злоупотреблять.
TILES = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"

#: Какой обзор показывать при сверке. z=16 при span=3 даёт участок около
#: полутора километров — на нём замер 12.09.2026 дал чистое разделение:
#: десять баллов верному месту и ноль трём чужим.
VERIFY_ZOOM = 16
VERIFY_SPAN = 3

#: Ниже какого балла версия считается неподтверждённой. Разделение оказалось
#: не пограничным, а полным (10 против 0), поэтому порог посередине и никакой
#: тонкой настройки не просит.
VERIFY_MIN = 5

#: Версии крупнее этого не сверяем: у города на снимке сверху нет той геометрии,
#: которую видно на фотографии, и сверка выродится в угадывание.
VERIFY_BELOW = 2_000.0

#: О чём спрашивать при сверке. Главное тут — предупредить о смене ракурса:
#: без этой оговорки модель искала на снимке СВЕРХУ горы на горизонте и на их
#: отсутствии отвечала «не совпадает» (замер 12.09.2026).
_VERIFY = {
    "ru": """Первая картинка — фотография, снятая с земли, обычным объективом.
Вторая — тот же мир, но СВЕРХУ: спутниковый снимок небольшого участка.

Ракурсы разные, и это главное. На снимке сверху по построению НЕ ВИДНО ни гор на
горизонте, ни неба, ни фасадов — их отсутствие ничего не доказывает. Сравнивать
можно только план: рисунок дорог и перекрёстков, форму крыш и расположение
построек, границу застройки и зелени, характерные объекты.

Ответь двумя строками:
СХОДСТВО: <число от 0 до 10, где 0 — ничего общего, 10 — точно это место>
ПОЧЕМУ: <одна короткая фраза>""",
    "en": """The first picture is a photo taken from the ground with an ordinary
lens. The second is the same world seen FROM ABOVE: a satellite view of a small
area.

The viewpoints differ, and that is the point. A top-down view by construction
shows no mountains on the horizon, no sky and no facades — their absence proves
nothing. Compare only the plan: roads and junctions, roof shapes and building
layout, the edge between built-up land and greenery, distinctive objects.

Answer in two lines:
MATCH: <a number from 0 to 10, where 0 is nothing in common and 10 is certainly
        this place>
WHY: <one short phrase>""",
}

#: Подпись строки с оценкой сходства.
_MATCH = ("сходство:", "match:")

#: Части адреса от точной к общей — для ответа по координатам из файла.
_ADDRESS = (
    "tourism", "attraction", "building", "amenity", "road",
    "suburb", "city", "town", "village", "county", "state", "country",
)


@dataclass(frozen=True, slots=True)
class Guess:
    """Одна версия модели: как называется и где, если она сказала."""

    name: str
    local: str = ""
    point: tuple[float, float] | None = None

    @property
    def queries(self) -> tuple[str, ...]:
        """Чем спрашивать геокодер, по порядку.

        Местное написание первым, и это не вежливость: «пляж Конъяалты, Анталия»
        и «отель Rixos Downtown Antalya» по-русски не находятся вовсе, а
        по-английски и по-турецки находятся (замер 12.09.2026).
        """
        names = [name for name in (self.local, self.name) if name]
        return tuple(dict.fromkeys(names))


@dataclass(frozen=True, slots=True)
class Reading:
    """Что модель вычитала из снимка: зацепки и версии."""

    clues: str = ""
    guesses: tuple[Guess, ...] = field(default_factory=tuple)

    @property
    def empty(self) -> bool:
        """Нечего проверять."""
        return not self.guesses


def is_refusal(answer: str) -> bool:
    """Отказалась ли модель называть место.

    Отказ надо отличать от ответа: «не знаю», отправленное в геокодер, вернёт
    какую-нибудь деревню Незнаево, и догадка превратится в уверенный ответ.
    """
    low = answer.strip().lower().lstrip("«\"'").strip()
    return not low or any(low.startswith(word) for word in _REFUSALS)


def clean_place(answer: str) -> str:
    """Снять с названия обрамление и знаки.

    Одним набором и с обоих концов: по отдельности точка и кавычка спасают друг
    друга — «Анталия».» теряло точку и оставляло кавычку.
    """
    first = answer.strip().splitlines()[0] if answer.strip() else ""
    return first.strip(_EDGES)


def parse_point(text: str) -> tuple[float, float] | None:
    """Координаты из свободной строки. ``None`` — их там нет.

    Модель пишет то «36.8969, 30.7133», то «нет». Берём только то, что похоже на
    пару чисел, и проверяем, что они на Земле: перепутанные местами широта и
    долгота иначе уехали бы в океан молча.
    """
    found = _POINT.match(text.strip().strip(_EDGES))
    if found is None:
        return None
    try:
        latitude = float(found.group(1).replace(",", "."))
        longitude = float(found.group(2).replace(",", "."))
    except ValueError:
        return None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    return latitude, longitude


def parse_reading(answer: str) -> Reading:
    """Разобрать лестницу ответа: зацепки и ступени от общего к частному.

    **Версии строятся от частного к общему**, и это порядок доверия: если модель
    честно заполнила МЕСТО, оно и проверяется первым; не заполнила — берётся
    район, потом город, потом страна. Пустая ступень означает «не знаю», и
    выдумывать за модель нечего.

    Ступень МЕСТО дополнительно просеивается: выдуманный адрес отбрасывается
    (см. `_FABRICATED`), и тогда ответом становится город — общий, но верный.
    """
    said: dict[str, str] = {}
    clues = ""
    for line in answer.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        for mark in _CLUES:
            if low.startswith(mark):
                clues = stripped[len(mark) :].strip(_EDGES)
        for field_name, marks in _FIELDS.items():
            for mark in marks:
                if low.startswith(mark):
                    value = stripped[len(mark) :].strip(_EDGES)
                    if value and not is_empty(value) and not is_refusal(value):
                        said[field_name] = value

    place = said.get("place", "")
    if place and _FABRICATED.search(place):
        # Выдуманный адрес вреднее честного города: он звучит точно и уводит
        # за десяток километров (живой прогон 12.09.2026).
        place = ""

    country, city = said.get("country", ""), said.get("city", "")
    district = said.get("district", "")
    steps: list[Guess] = []
    if place:
        local = said.get("local", "")
        steps.append(Guess(name=place, local=_with_city(local, city)))
    if district:
        steps.append(Guess(name=_with_city(district, city) or district))
    if city:
        steps.append(Guess(name=_with_city(city, country) or city))
    elif country:
        steps.append(Guess(name=country))
    return Reading(clues=clues, guesses=tuple(steps[:MAX_CANDIDATES]))



def _coordinates(found: dict[str, Any]) -> tuple[float, float] | None:
    """Точка из ответа геокодера."""
    try:
        return float(found["lat"]), float(found["lon"])
    except (KeyError, TypeError, ValueError):
        return None


def _inside(area: dict[str, Any] | None, point: tuple[float, float]) -> bool:
    """Лежит ли точка внутри найденной области. Нет области — верим на слово.

    Запас в `MARGIN` не от неточности рамки, а от края: снимок с окраины города
    вполне сделан за его границей, и отбрасывать такую версию было бы неверно.
    """
    if area is None:
        return True
    box = area.get("boundingbox")
    try:
        south, north, west, east = (float(value) for value in box)
    except (TypeError, ValueError):
        return True
    margin = MARGIN / 111_320
    return (south - margin <= point[0] <= north + margin
            and west - margin <= point[1] <= east + margin)


def _with_city(name: str, wider: str) -> str:
    """Приписать к названию то, что шире, если его там ещё нет.

    Геокодеру «Коньяалты» без города найдётся где угодно, а «Коньяалты,
    Анталья» — там, где нужно.
    """
    if not name:
        return ""
    if not wider or wider.lower() in name.lower():
        return name
    return f"{name}, {wider}"


def span_metres(box: Any) -> float | None:
    """Размер найденного объекта по большей стороне, метров.

    Рамка приходит как «юг, север, запад, восток» в градусах. Долгота к полюсам
    сжимается, поэтому её умножаем на косинус широты — иначе объект в Норвегии
    выглядел бы вдвое шире, чем он есть.
    """
    try:
        south, north, west, east = (float(value) for value in box)
    except (TypeError, ValueError):
        return None
    middle = math.radians((south + north) / 2)
    tall = abs(north - south) * 111_320
    wide = abs(east - west) * 111_320 * math.cos(middle)
    return max(tall, wide)


def precision_of(found: dict[str, Any]) -> float | None:
    """Насколько точен ответ геокодера, метров. ``None`` — судить нечем.

    Два источника, и порядок между ними важен:

    * **Протяжённый объект** (линия или область) сам говорит о своём размере
      рамкой, и точнее этого не скажешь: у Красной площади 359 метров, у Твери
      двадцать один километр.
    * **Точечный объект** о размере не говорит ничего: рамка у него всегда
      одиннадцать метров — и у отеля, и у семикилометрового пляжа, и у
      Средиземного моря (замер 12.09.2026). Тут судим по рангу.

    Ошибка в оставшемся случае возможна и признаётся: пляж, отмеченный на карте
    точкой, получит «до здания», хотя тянется на километры. Цена мала — таких
    объектов немного, а обратная ошибка (объявить отель городом) обесценила бы
    ответ целиком.
    """
    rank = found.get("place_rank")
    box = span_metres(found.get("boundingbox"))
    if str(found.get("osm_type", "")).lower() in ("way", "relation") and box is not None:
        return box
    return metres_for_rank(rank)


def metres_for_rank(rank: Any) -> float | None:
    """Во что превращается ранг геокодера. ``None`` — ранга нет."""
    try:
        value = int(rank)
    except (TypeError, ValueError):
        return None
    for edge, metres in RANK_METRES:
        if value >= edge:
            return metres
    return None


def describe_precision(metres: float | None, language: str = "ru") -> str:
    """Как назвать вслух достигнутую точность. Пусто — точность неизвестна."""
    if metres is None:
        return ""
    for limit, russian, english in PRECISION:
        if metres <= limit:
            return english if language == "en" else russian
    return ""


def tighter(candidate: float | None, current: float | None) -> bool:
    """Точнее ли новая версия прежней.

    Неизвестная точность хуже любой известной: выбирать вслепую нечего.
    """
    if candidate is None:
        return False
    return current is None or candidate < current


def map_url(latitude: float, longitude: float) -> str:
    """Ссылка на точку в OpenStreetMap."""
    return (
        f"https://www.openstreetmap.org/?mlat={latitude:.6f}"
        f"&mlon={longitude:.6f}#map=17/{latitude:.6f}/{longitude:.6f}"
    )


def spoken_address(answer: dict[str, Any]) -> str:
    """Короткое название места из ответа геокодера — то, что скажут вслух."""
    address = answer.get("address")
    parts: list[str] = []
    if isinstance(address, dict):
        for key in _ADDRESS:
            value = address.get(key)
            if isinstance(value, str) and value and value not in parts:
                parts.append(value)
            if len(parts) == 3:
                break
    if parts:
        return ", ".join(parts)
    name = answer.get("display_name")
    return ", ".join(str(name).split(", ")[:3]) if name else ""


def coordinates_of(path: Path) -> tuple[float, float] | None:
    """Координаты съёмки из EXIF. ``None`` — их там нет, и это обычное дело.

    Через Pillow, а не своим разбором TIFF: Pillow и так нужен этому скиллу,
    чтобы показать картинку модели.
    """
    try:
        from PIL import ExifTags, Image

        with Image.open(path) as picture:
            gps = picture.getexif().get_ifd(ExifTags.IFD.GPSInfo)
    except Exception:  # noqa: BLE001 — битый файл не повод падать, просто нет координат
        return None
    if not gps:
        return None
    try:
        latitude = _degrees(gps[2], gps[1])
        longitude = _degrees(gps[4], gps[3])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    return round(latitude, 6), round(longitude, 6)


def _degrees(parts: Any, reference: Any) -> float:
    """Градусы, минуты и секунды EXIF — в одно число со знаком."""
    degrees, minutes, seconds = (float(part) for part in parts)
    value = degrees + minutes / 60 + seconds / 3600
    return -value if str(reference).strip().upper() in ("S", "W") else value


def picture_for_model(path: Path, *, limit: int = LIMIT) -> tuple[str, tuple[int, int]]:
    """Картинка из файла в виде ``data:``-URI для зрячей модели.

    Уменьшаем только то, что больше предела: у зрения замерено, что на участке
    от 768 до 2200 пикселей цена в токенах не меняется, а читаемость от сжатия
    портится всерьёз. Читаемость тут и есть точность: место узнают по вывеске и
    по силуэту гор, и то и другое сжатие съедает первым.
    """
    from PIL import Image

    with Image.open(path) as picture:
        picture = picture.convert("RGB")
        size = picture.size
        if max(size) > limit:
            scale = limit / max(size)
            size = (max(1, int(size[0] * scale)), max(1, int(size[1] * scale)))
            picture = picture.resize(size, Image.LANCZOS)
        buffer = io.BytesIO()
        picture.save(buffer, format="JPEG", quality=92)
    body = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{body}", size


def tile_of(latitude: float, longitude: float, zoom: int) -> tuple[float, float]:
    """Номер тайла для точки в обычной схеме карт (Web Mercator)."""
    count = 2 ** zoom
    x = (longitude + 180.0) / 360.0 * count
    y = (1 - math.asinh(math.tan(math.radians(latitude))) / math.pi) / 2 * count
    return x, y


def stitch(tiles: dict[tuple[int, int], bytes], span: int) -> bytes:
    """Склеить сетку тайлов в одну картинку и отдать её JPEG.

    Недостающий тайл оставляем серым, а не отменяем сверку: край области или
    один сбойный запрос не повод отказываться от проверки целиком.
    """
    from PIL import Image

    canvas = Image.new("RGB", (256 * span, 256 * span), (128, 128, 128))
    for (column, row), body in tiles.items():
        with Image.open(io.BytesIO(body)) as piece:
            canvas.paste(piece.convert("RGB"), (column * 256, row * 256))
    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def read_match(answer: str) -> int | None:
    """Оценка сходства из ответа модели. ``None`` — оценки нет."""
    for line in answer.splitlines():
        low = line.strip().lower()
        for mark in _MATCH:
            if low.startswith(mark):
                digits = re.search(r"\d+", low[len(mark) :])
                if digits:
                    return max(0, min(10, int(digits.group())))
    return None


def resolve_photo(path: str) -> Path | None:
    """Файл фотографии по сказанному пути. ``None`` — не нашли или не картинка."""
    cleaned = path.strip().strip('"').strip("'")
    if not cleaned:
        return None
    photo = Path(cleaned).expanduser()
    if not photo.is_file() or photo.suffix.lower() not in PICTURES:
        return None
    return photo


class PhotoPlaceSkill(Skill):
    """Называет место съёмки: по виду снимка, а при удаче — по координатам."""

    meta = SkillMeta(
        name="photo_place",
        description="Где снята фотография: на экране или в файле.",
        version="0.4.0",
        spoken=("место по фото", "где снято", "photo place"),
    )

    async def on_setup(self) -> None:
        """Приготовить клиент геокодера."""
        self._client: httpx.AsyncClient | None = None

    async def on_stop(self) -> None:
        """Закрыть соединения."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        """Один клиент на весь скилл."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=GEOCODE_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
        return self._client

    @tool(
        phrases=[
            "где снято это фото",
            "где снята эта фотография",
            "где сделана эта фотография",
            "где снято",
            "определи место по фотографии",
            "что это за место на фото",
            "where was this photo taken",
        ],
        reversible=False,
    )
    async def photo_place(
        self, path: str = "", hint: str = "", language: str = "ru"
    ) -> ToolResult:
        """Назвать место, где снята фотография, как можно точнее.

        Пустой путь означает «то, что сейчас на экране»: чаще всего снимок
        показывают именно так — в мессенджере или в браузере.

        Помечен необратимым по той же причине, что и зрение: картинка уходит в
        чужое облако, и делать это шагом плана без спроса нельзя.

        :param path: путь к файлу; пусто — смотреть на экран.
        :param hint: что владелец знает о снимке и чего на нём не видно: «это
            Турция», «не Анталия». Сужает поиск сильнее любых зацепок.
        :param language: язык ответа.
        """
        code = "en" if str(language).startswith("en") else "ru"
        if path.strip():
            return await self._by_file(path, code, hint)
        return await self._by_screen(code, hint)

    @tool(
        phrases=[
            "открой место съёмки на карте",
            "покажи на карте где снято",
            "открой это место в картах",
            "show where this was taken on the map",
        ],
        reversible=False,
    )
    async def place_on_map(
        self, path: str = "", hint: str = "", language: str = "ru"
    ) -> ToolResult:
        """Найти место съёмки и открыть его в картах.

        :param path: путь к файлу; пусто — смотреть на экран.
        :param hint: что владелец знает о снимке и чего на нём не видно.
        :param language: язык ответа.
        """
        found = await self.photo_place(path=path, hint=hint, language=language)
        if not found.ok:
            return found
        value = found.value if isinstance(found.value, dict) else {}
        url = value.get("map_url")
        if not url:
            return ToolResult.failure(
                "место названо, а точки на карте нет",
                speech={
                    "ru": "Место назвал, а на карте показать не смог.",
                    "en": "I named the place but could not put it on the map.",
                },
            )
        if not self.tools.has("browser.open_site"):
            return found
        await self.tools.invoke("browser.open_site", {"site": url})
        place = value.get("place") or ""
        return ToolResult.success(
            value,
            speech={
                "ru": f"Открыл на карте: {place}." if place else "Открыл на карте.",
                "en": f"Opened on the map: {place}." if place else "Opened on the map.",
            },
        )

    async def health(self) -> HealthStatus:
        """Здоров, пока есть Pillow и зрячая модель: геокодер — дополнение."""
        try:
            from PIL import Image  # noqa: F401  # проверяем наличие, не зовём
        except ImportError:
            return HealthStatus.degraded("нет Pillow: pip install pillow")
        if not self.context.llm.available:
            return HealthStatus.degraded("модель не настроена: нет ключа")
        try:
            self.context.llm.profiles.get(VISION_TASK)
        except LLMNotConfigured:
            return HealthStatus.degraded(
                f"нет профиля {VISION_TASK!r} в llm.profiles конфига"
            )
        return HealthStatus.healthy()

    # --- откуда берём картинку ---------------------------------------------

    async def _by_screen(self, code: str, hint: str) -> ToolResult:
        """Спросить у зрения про то, что на экране.

        Своего снимка экрана не делаем: этим занимается скилл `screen`, и
        заводить второй захват значило бы иметь два разных ответа на вопрос
        «что видно».
        """
        if not self.tools.has("screen.look"):
            return ToolResult.failure(
                "нет скилла зрения",
                speech={
                    "ru": "Не вижу экран: модуль зрения не подключён.",
                    "en": "I cannot see the screen: the vision module is missing.",
                },
            )
        looked = await self.tools.invoke(
            "screen.look", {"question": self._question(code, hint)}
        )
        if not looked.ok:
            return looked
        return await self._answer(str(looked.value or ""), code)

    async def _by_file(self, path: str, code: str, hint: str) -> ToolResult:
        """Разобрать файл: сперва координаты, если они есть, потом вид."""
        photo = resolve_photo(path)
        if photo is None:
            return ToolResult.failure(
                f"файл не найден или это не фотография: {path!r}",
                speech={
                    "ru": "Не нашёл такую фотографию.",
                    "en": "I could not find that photo.",
                },
            )
        exact = await asyncio.to_thread(coordinates_of, photo)
        if exact is not None:
            # Координаты в файле — редкая удача, зато точная.
            return await self._by_coordinates(exact, photo)

        try:
            image, _ = await asyncio.to_thread(picture_for_model, photo)
        except Exception as exc:  # noqa: BLE001 — битый файл не повод падать
            return ToolResult.failure(
                f"не удалось прочитать {photo.name}: {exc}",
                speech={
                    "ru": "Не смог открыть эту фотографию.",
                    "en": "I could not open that photo.",
                },
            )
        said = await self._ask_model(image, code, hint)
        if said is None:
            return ToolResult.failure(
                "зрячая модель не ответила",
                speech={
                    "ru": "Модель не ответила про эту фотографию.",
                    "en": "The model did not answer about this photo.",
                },
            )
        return await self._answer(said, code, photo=image)

    def _question(self, code: str, hint: str) -> str:
        """Что спросить у зрения, с учётом подсказки владельца."""
        asked = _ASK[code]
        clue = hint.strip()
        if not clue:
            return asked
        # Подсказка идёт первой строкой: человек знает о снимке то, чего на нём
        # не видно, и это сужает поиск сильнее любых зацепок с картинки.
        head = "Владелец подсказывает" if code == "ru" else "The owner says"
        return f"{head}: {clue}\n\n{asked}"

    async def _ask_model(self, image: str, code: str, hint: str) -> str | None:
        """Показать картинку зрячей модели и получить зацепки с версиями."""
        messages = [Message.user(self._question(code, hint), images=(image,))]
        try:
            response = await self.context.llm.complete(messages, task=VISION_TASK)
        except Exception as exc:  # noqa: BLE001 — сеть и тариф, не наша вина
            self.log.warning("Зрячая модель не ответила: %s", exc)
            return None
        return response.text.strip()

    # --- что делаем с версиями -----------------------------------------------

    async def _answer(self, said: str, code: str, *, photo: str = "") -> ToolResult:
        """Выбрать версию по лестнице и, если есть чем, сверить её со спутником."""
        if is_refusal(said):
            return ToolResult.failure(
                "модель места не узнала",
                speech={
                    "ru": "По этой фотографии место не узнаю.",
                    "en": "I cannot tell where this was taken.",
                },
            )
        reading = parse_reading(said)
        if reading.empty:
            # Формат не соблюдён, но ответ есть, и терять его нельзя: берём
            # первую строку как единственную версию.
            reading = Reading(guesses=(Guess(name=clean_place(said)),))
        if reading.clues:
            self.log.info("Зацепки на снимке: %s", reading.clues)

        best, checked = await self._checked(reading.guesses, photo, code)
        if best is None:
            return ToolResult.failure(
                "версии не подтвердились",
                speech={
                    "ru": "Место назвать не берусь, ничего не сходится.",
                    "en": "I would rather not guess, nothing checks out.",
                },
            )
        guess, point, metres = best
        accuracy = describe_precision(metres, code)
        payload: dict[str, Any] = {
            "place": guess.name,
            "local": guess.local,
            "clues": reading.clues,
            "guesses": [item.name for item in reading.guesses],
            "exact": False,
            "checked": checked,
            "accuracy_m": round(metres) if metres is not None else None,
            "latitude": point[0] if point else None,
            "longitude": point[1] if point else None,
            "map_url": map_url(*point) if point else "",
        }
        self.log.info(
            "Место по снимку: %r, точка %s, точность %s м (версии: %s)",
            guess.name,
            point,
            round(metres) if metres is not None else "?",
            ", ".join(item.name for item in reading.guesses),
        )
        if point is None:
            return ToolResult.success(
                payload,
                speech={
                    "ru": f"Похоже на {guess.name}. На карте показать не смогу.",
                    "en": f"Looks like {guess.name}. I cannot put it on the map though.",
                },
            )
        # Точность говорится вслух: владельцу нужна точка, и услышать «только до
        # города» ему важнее, чем услышать название города.
        tail = f", {accuracy}" if accuracy else ""
        # Сверенная версия — уже не догадка, и говорить о ней надо иначе.
        if checked:
            return ToolResult.success(
                payload,
                speech={
                    "ru": f"{guess.name}{tail}. Сверил со спутником, сходится.",
                    "en": f"{guess.name}{tail}. Checked against satellite, it matches.",
                },
            )
        return ToolResult.success(
            payload,
            speech={
                "ru": f"Похоже на {guess.name}{tail}.",
                "en": f"Looks like {guess.name}{tail}.",
            },
        )

    async def _checked(
        self, guesses: tuple[Guess, ...], photo: str, code: str
    ) -> tuple[tuple[Guess, tuple[float, float] | None, float | None] | None, bool]:
        """Выбрать версию и, если она достаточно точная, сверить со спутником.

        Не сошлась — версия отбрасывается, и берётся следующая ступень, более
        общая. Именно этого шага не хватало весь день: «остановка EXPO» и
        «здание муниципалитета» звучали точно, находились на карте и уводили на
        десять километров. Сверка отвечает на единственный вопрос, которого не
        задавали, — а похоже ли вообще.

        Общие версии (город, область) не сверяются: у города на снимке сверху
        нет той геометрии, которую видно на фотографии.
        """
        remaining = guesses
        while remaining:
            best = await self._weigh(remaining)
            if best is None:
                return None, False
            guess, point, metres = best
            precise = point is not None and metres is not None and metres <= VERIFY_BELOW
            if not photo or not precise:
                return best, False
            score = await self._verify(photo, point, code)  # type: ignore[arg-type]
            if score is None or score >= VERIFY_MIN:
                # Сверка не состоялась — это не повод отвергать версию: молчание
                # спутника ничего не доказывает, в отличие от его «не похоже».
                return best, score is not None
            self.log.info("Версия %r со спутником не сошлась — беру следующую", guess.name)
            index = remaining.index(guess)
            remaining = remaining[index + 1 :]
        return None, False

    async def _weigh(
        self, guesses: tuple[Guess, ...]
    ) -> tuple[Guess, tuple[float, float] | None, float | None] | None:
        """Пройти ступени от частного к общему и взять первую, что нашлась.

        **Порядок ступеней — это порядок доверия модели, и спорить с ним не
        нужно.** Прежняя версия выбирала ту версию, что нашлась на карте
        точнее, и это оказалось ровно наоборот: выдуманный «перекрёсток D400»
        находился как объект на сто метров и побеждал честный город, промахиваясь
        на двенадцать километров (живой прогон 12.09.2026).

        **Частное обязано лежать внутри общего.** Самая широкая ступень (город
        или страна) ищется первой и служит границей: вывеска, найденная в другом
        конце страны, отбрасывается. Без этой проверки любое совпадение названия
        уводит ответ куда угодно — именно так «EXPO 2016» нашлось остановкой
        трамвая в десяти километрах от места.
        """
        if not guesses:
            return None
        area = await self._find(guesses[-1]) if len(guesses) > 1 else None
        for guess in guesses[:-1] if area is not None else guesses:
            found = await self._find(guess)
            if found is None:
                if guess.point is not None and _inside(area, guess.point):
                    return guess, guess.point, None
                continue
            point = _coordinates(found)
            if point is None or not _inside(area, point):
                self.log.debug("Версия %r нашлась вне города — отбрасываю", guess.name)
                continue
            return guess, point, precision_of(found)
        if area is None:
            return None
        point = _coordinates(area)
        return (guesses[-1], point, precision_of(area)) if point else None

    async def _by_coordinates(
        self, point: tuple[float, float], photo: Path
    ) -> ToolResult:
        """Ответ по координатам из файла — единственный случай, когда мы знаем."""
        answer = await self._named(point)
        place = spoken_address(answer) if answer else ""
        payload = {
            "place": place,
            "exact": True,
            "accuracy_m": 10,
            "latitude": point[0],
            "longitude": point[1],
            "map_url": map_url(*point),
            "file": str(photo),
        }
        self.log.info("Координаты из EXIF: %s -> %r", point, place)
        if not place:
            return ToolResult.success(
                payload,
                speech={
                    "ru": "Координаты в снимке есть, а названия места не нашёл.",
                    "en": "The photo has coordinates but I found no place name.",
                },
            )
        return ToolResult.success(
            payload,
            speech={
                "ru": f"Снято здесь: {place}. Это из самого снимка, точно.",
                "en": f"Taken here: {place}. That is from the photo itself, exact.",
            },
        )

    # --- сверка со спутником -------------------------------------------------

    async def _satellite(self, point: tuple[float, float]) -> str | None:
        """Спутниковый вид вокруг точки одной картинкой. ``None`` — не вышло."""
        centre_x, centre_y = tile_of(*point, VERIFY_ZOOM)
        left, top = int(centre_x) - VERIFY_SPAN // 2, int(centre_y) - VERIFY_SPAN // 2
        pieces: dict[tuple[int, int], bytes] = {}
        for column in range(VERIFY_SPAN):
            for row in range(VERIFY_SPAN):
                url = TILES.format(z=VERIFY_ZOOM, x=left + column, y=top + row)
                try:
                    response = await self._http().get(url)
                    response.raise_for_status()
                except httpx.HTTPError as error:
                    self.log.debug("Тайл %s не пришёл: %s", url, error)
                    continue
                pieces[(column, row)] = response.content
        if not pieces:
            return None
        body = await asyncio.to_thread(stitch, pieces, VERIFY_SPAN)
        return f"data:image/jpeg;base64,{base64.b64encode(body).decode('ascii')}"

    async def _verify(self, photo: str, point: tuple[float, float], code: str) -> int | None:
        """Сверить фотографию со спутниковым видом точки. ``None`` — не удалось.

        Это тот шаг, которого не хватало весь день: догадка перестаёт быть
        догадкой, когда её проверили. Замер 12.09.2026 на Дмитровском кремле дал
        чистое разделение — десять баллов верному месту и ноль трём чужим.
        """
        view = await self._satellite(point)
        if view is None:
            return None
        try:
            response = await self.context.llm.complete(
                [Message.user(_VERIFY[code], images=(photo, view))], task=VISION_TASK
            )
        except Exception as exc:  # noqa: BLE001 — сеть и тариф, не наша вина
            self.log.warning("Сверка не состоялась: %s", exc)
            return None
        score = read_match(response.text)
        self.log.info("Сверка со спутником: %s из 10", score if score is not None else "?")
        return score

    # --- геокодер ------------------------------------------------------------

    async def _find(self, guess: Guess) -> dict[str, Any] | None:
        """Название — в объект на карте. ``None`` — геокодер такого не знает."""
        for query in guess.queries:
            try:
                response = await self._http().get(
                    f"{NOMINATIM}/search",
                    params={"q": query, "format": "jsonv2", "limit": 1},
                )
                response.raise_for_status()
                found = response.json()
            except (httpx.HTTPError, ValueError) as error:
                self.log.warning("Геокодер не ответил на %r: %s", query, error)
                return None
            if isinstance(found, list) and found and isinstance(found[0], dict):
                return found[0]
        return None

    async def _named(self, point: tuple[float, float]) -> dict[str, Any] | None:
        """Точка — в название. ``None`` — геокодер промолчал."""
        try:
            response = await self._http().get(
                f"{NOMINATIM}/reverse",
                params={
                    "lat": f"{point[0]:.6f}",
                    "lon": f"{point[1]:.6f}",
                    "format": "jsonv2",
                    "zoom": 18,
                    "accept-language": "ru,en",
                },
            )
            response.raise_for_status()
            answer = response.json()
        except (httpx.HTTPError, ValueError) as error:
            self.log.warning("Геокодер не назвал точку %s: %s", point, error)
            return None
        return answer if isinstance(answer, dict) else None
