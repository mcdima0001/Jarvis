"""Где снято: место по фотографии — на экране или в файле.

Скилл появился из живого разбора 12.09.2026. Владелец показал на снимок в
Telegram и попросил найти место; ассистент описал картинку, но места не назвал,
а открыть его в картах не смог вовсе.

**Основа — содержимое снимка, а не EXIF.** Первая версия скилла была построена
на координатах из файла, и владелец сразу возразил: «EXIF не всегда есть».
Проверка подтвердила буквально — из сорока снимков на его машине координат не
оказалось **ни у одного**. Мессенджеры и соцсети вырезают GPS при отправке, у
скриншота его не бывает по построению, а именно скриншоты и пересланные
фотографии чаще всего и показывают ассистенту.

**Нужна точка, а не город.** Второе возражение владельца: «город я и сам найду».
Оно и определило устройство. Человек, который ищет место всерьёз, тратит
двадцать минут и делает три вещи: перечисляет зацепки (язык вывесок, рельеф,
растительность, разметка, номера машин), выдвигает несколько версий и проверяет
их. Модель, которую спрашивают «где это снято», отвечает с первого взгляда и
потому называет страну. Поэтому её просят пройти тот же путь: **сперва зацепки,
потом до трёх версий, и каждая — точкой**.

**Точность измеряется, а не обещается.** Геокодер отдаёт рамку найденного
объекта, и по её размеру видно, что именно нашлось: здание — сорок метров,
башня — сто семьдесят, площадь — триста шестьдесят, город — двадцать километров
(замер 12.09.2026). Из версий побеждает та, что нашлась **точнее**, а не та, что
первая. И вслух говорится, до чего дотянулись: «с точностью до здания» или
«только до города». Владельцу это важнее названия: город он и сам найдёт.

**Догадку и замер не путаем.** Координаты из файла — это «снято здесь»,
узнавание по виду — «похоже на». Разница не косметическая: модель уверенно
называет Анталию по любому средиземноморскому пейзажу.
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

#: Сколько версий проверять. Больше трёх — это уже не проверка версий, а
#: перебор, и каждая стоит запроса к чужому сервису.
MAX_CANDIDATES = 3

#: Длинная сторона картинки для модели. Тот же предел, что у зрения.
LIMIT = 1920

#: Что читаем как фотографию. EXIF бывает только в части форматов, но зрение
#: работает с любым, поэтому список шире.
PICTURES = frozenset({".jpg", ".jpeg", ".jpe", ".png", ".webp", ".tif", ".tiff", ".bmp"})

_ASK = {
    "ru": """Определи, где снята эта фотография. Отвечай как человек, который ищет
место всерьёз, а не с первого взгляда.

Сначала перечисли зацепки, которые видишь: язык и текст на вывесках, стиль
архитектуры, рельеф и силуэт гор, растительность, дорожная разметка и знаки,
номера машин, тип столбов и ограждений, положение солнца.

Потом назови до трёх версий, от самой вероятной к запасной. Версия — это ТОЧКА,
а не город: здание, отель, пляж, набережная, смотровая площадка, перекрёсток,
достопримечательность.

Ответь строго в таком виде, без пояснений:
ЗАЦЕПКИ: <через запятую>
1) <название по-русски> | <название на местном языке или по-английски> | <широта, долгота или нет>
2) <то же самое для второй версии>
3) <то же самое для третьей версии>

Узнаёшь только город — поставь город версией, но улицу не выдумывай.
Не узнаёшь вовсе — ответь ровно: не знаю.""",
    "en": """Work out where this photo was taken. Answer like a person who looks
into it properly, not at first glance.

First list the clues you can see: language and text on signs, architecture,
terrain and mountain silhouette, vegetation, road markings and signs, number
plates, poles and railings, the position of the sun.

Then give up to three guesses, best first. A guess is a SPOT, not a city: a
building, hotel, beach, promenade, viewpoint, crossroads or landmark.

Answer exactly like this, with no commentary:
CLUES: <comma separated>
1) <name in English> | <name in the local language> | <latitude, longitude or no>
2) <the same for the second guess>
3) <the same for the third guess>

If you only recognise the city, put the city as a guess, but do not invent a
street. If you cannot tell at all, answer exactly: unknown.""",
}

#: Подпись строки с зацепками. Обе раскладки: модель отвечает на языке вопроса.
_CLUES = ("зацепки:", "clues:")

#: Строка версии: «1) название | местное | координаты». Номер обязателен — без
#: него в разбор лезут вступления вроде «вот мои версии».
_GUESS = re.compile(r"^\s*(\d)\s*[).]\s*(.+)$")

#: Координаты в свободном виде: «36.8969, 30.7133». Знак и дробная часть
#: необязательны, разделитель — запятая или точка с запятой.
_POINT = re.compile(r"^\s*(-?\d{1,3}(?:[.,]\d+)?)\s*[;,]\s*(-?\d{1,3}(?:[.,]\d+)?)\s*$")

#: Что срезать с краёв названия: обрамление и знаки, но не буквы.
_EDGES = " \t«»\"'`.,:;!?"

#: Чем модель отказывается. Проверяется началом строки.
_REFUSALS = ("не знаю", "не могу", "непонятно", "unknown", "i cannot", "i can't", "unable")

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
    """Разобрать ответ модели: строка зацепок и пронумерованные версии.

    Разбор терпим к мелочам оформления и строг к сути: версия без названия не
    версия, а третье поле (координаты) модель нет-нет да и опустит.
    """
    clues = ""
    guesses: list[Guess] = []
    for line in answer.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        for mark in _CLUES:
            if low.startswith(mark):
                clues = stripped[len(mark) :].strip(_EDGES)
                break
        found = _GUESS.match(stripped)
        if found is None:
            continue
        parts = [part.strip(_EDGES) for part in found.group(2).split("|")]
        name = parts[0] if parts else ""
        if not name or is_refusal(name):
            continue
        local = parts[1] if len(parts) > 1 else ""
        point = parse_point(parts[2]) if len(parts) > 2 else None
        guesses.append(Guess(name=name, local=local, point=point))
    return Reading(clues=clues, guesses=tuple(guesses[:MAX_CANDIDATES]))


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
        version="0.3.0",
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
        return await self._answer(said, code)

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

    async def _answer(self, said: str, code: str) -> ToolResult:
        """Проверить версии геокодером и выбрать ту, что нашлась точнее."""
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

        best = await self._weigh(reading.guesses)
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
        return ToolResult.success(
            payload,
            speech={
                "ru": f"Похоже на {guess.name}{tail}.",
                "en": f"Looks like {guess.name}{tail}.",
            },
        )

    async def _weigh(
        self, guesses: tuple[Guess, ...]
    ) -> tuple[Guess, tuple[float, float] | None, float | None] | None:
        """Проверить версии и вернуть лучшую: название, точку и точность.

        **Побеждает та, что нашлась точнее, а не первая.** Модель ставит первой
        самую вероятную, но вероятная и точная — разные вещи: «Анталия» вернее
        «отеля Rixos», а толку от неё меньше.

        Координаты самой модели идут в дело, когда геокодер названия не знает:
        «вон та бухта» именем не ищется, а точкой — да. Точность в этом случае
        неизвестна, и о ней честно молчим, а не выдумываем число.
        """
        best: tuple[Guess, tuple[float, float] | None, float | None] | None = None
        for guess in guesses:
            found = await self._find(guess)
            if found is not None:
                try:
                    point = (float(found["lat"]), float(found["lon"]))
                except (KeyError, TypeError, ValueError):
                    continue
                metres = precision_of(found)
                if best is None or tighter(metres, best[2]):
                    best = (guess, point, metres)
                continue
            if guess.point is not None and best is None:
                best = (guess, guess.point, None)
        return best

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
