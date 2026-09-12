"""Где снято: место по фотографии — на экране или в файле.

Скилл появился из живого разбора 12.09.2026. Владелец показал на снимок в
Telegram и попросил найти место; ассистент описал картинку, но места не назвал,
а открыть его в картах не смог вовсе.

**Основа тут — содержимое снимка, а не EXIF, и это главное решение.** Первая
версия скилла была построена на координатах из файла, и владелец сразу возразил:
«EXIF не всегда есть». Проверка это подтвердила буквально — из сорока снимков на
его машине координат не оказалось **ни у одного**. Мессенджеры и соцсети вырезают
GPS при отправке, у скриншота его не бывает по построению, а именно скриншоты и
пересланные фотографии чаще всего и показывают ассистенту.

Поэтому порядок обратный привычному:

1. **Смотрим на картинку зрячей моделью** — она узнаёт место по виду: горы,
   архитектура, вывески, тип застройки. Это работает на любом снимке, включая то,
   что открыто на экране прямо сейчас.
2. **Название превращаем в точку** обратным запросом к геокодеру, чтобы вышла
   ссылка на карту, а не просто слово.
3. **EXIF спрашиваем первым, если это файл** — не потому, что он есть, а потому,
   что когда он есть, он точен. Это удача, а не опора.

**Догадку и замер не путаем.** Координаты из файла — это «снято здесь»,
узнавание по виду — «похоже на». Разница не косметическая: модель уверенно
называет Анталию по любому средиземноморскому пейзажу, и выдавать это за
измерение нельзя.
"""

from __future__ import annotations

import asyncio
import base64
import io
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

#: Геокодер OpenStreetMap: без ключа, по названию отдаёт точку и наоборот.
NOMINATIM = "https://nominatim.openstreetmap.org"

#: Представляться геокодеру обязательно: их правила требуют узнаваемого имени,
#: анонимные запросы блокируют.
USER_AGENT = "Jarvis voice assistant (github.com/mcdima0001/Jarvis)"

#: Сколько ждать геокодер. Ответ нужен внутри голосовой команды, а не когда-нибудь.
GEOCODE_TIMEOUT = 8.0

#: Длинная сторона картинки для модели. Тот же предел, что у зрения: дальше цена
#: в токенах растёт, а читаемость нет.
LIMIT = 1920

#: Что читаем как фотографию. EXIF бывает только в этих форматах, но зрение
#: работает с любым, поэтому список шире.
PICTURES = frozenset({".jpg", ".jpeg", ".jpe", ".png", ".webp", ".tif", ".tiff", ".bmp"})

#: О чём спрашивать модель. Просим **одну строку с названием**, а не рассказ:
#: ответ идёт в геокодер, и лишние слова там только мешают. Отдельно разрешаем
#: честно не знать — иначе модель угадывает всегда.
_ASK = {
    "ru": (
        "Посмотри на фотографию и назови место, где она снята, как можно точнее: "
        "город и страну, а если узнаёшь конкретное место — его название. "
        "Ответь ОДНОЙ строкой, только название места, без пояснений. "
        "Не узнаёшь — ответь ровно «не знаю»."
    ),
    "en": (
        "Look at the photo and name the place where it was taken, as precisely as "
        "you can: city and country, and the landmark if you recognise one. "
        "Answer in ONE line, the place name only, no explanations. "
        "If you cannot tell, answer exactly \"unknown\"."
    ),
}

#: Что срезать с краёв названия: обрамление и знаки, но не буквы.
_EDGES = " 	«»\"'`.,:;!?"

#: Чем модель отказывается. Проверяется началом строки: «не знаю», «не могу
#: определить», «unknown place» — всё это отказы.
_REFUSALS = ("не знаю", "не могу", "непонятно", "unknown", "i cannot", "i can't", "unable")

#: Части адреса от точной к общей — для ответа по координатам из файла.
_ADDRESS = (
    "tourism", "attraction", "building", "amenity", "road",
    "suburb", "city", "town", "village", "county", "state", "country",
)


def is_refusal(answer: str) -> bool:
    """Отказалась ли модель называть место.

    Отказ надо отличать от ответа: «не знаю», отправленное в геокодер, вернёт
    какую-нибудь деревню Незнаево, и догадка превратится в уверенный ответ.
    """
    low = answer.strip().lower().lstrip("«\"'").strip()
    return not low or any(low.startswith(word) for word in _REFUSALS)


def clean_place(answer: str) -> str:
    """Привести ответ модели к названию, которое можно спросить у геокодера.

    Модель просят ответить одной строкой, но она нет-нет да добавит вступление.
    Берём первую строку и снимаем кавычки с точкой — остальное не трогаем:
    выкусывать «лишнее» из названия места опаснее, чем оставить.
    """
    first = answer.strip().splitlines()[0] if answer.strip() else ""
    # Снимаем обрамление одним набором и с обоих концов: по отдельности точка и
    # кавычка спасают друг друга — «Анталия».» теряло точку и оставляло кавычку.
    return first.strip(_EDGES)


def map_url(latitude: float, longitude: float) -> str:
    """Ссылка на точку в OpenStreetMap."""
    return (
        f"https://www.openstreetmap.org/?mlat={latitude:.6f}"
        f"&mlon={longitude:.6f}#map=15/{latitude:.6f}/{longitude:.6f}"
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
    чтобы показать картинку модели, а сто тридцать строк ручного разбора
    двоичного формата — это сто тридцать строк, которые некому чинить.
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
    от 768 до 2200 пикселей цена в токенах не меняется вовсе, а читаемость от
    сжатия портится всерьёз.
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
        picture.save(buffer, format="JPEG", quality=88)
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
        version="0.2.0",
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
    async def photo_place(self, path: str = "", language: str = "ru") -> ToolResult:
        """Назвать место, где снята фотография.

        Пустой путь означает «то, что сейчас на экране»: чаще всего снимок
        показывают именно так — в мессенджере или в браузере.

        Помечен необратимым по той же причине, что и зрение: картинка уходит в
        чужое облако, и делать это шагом плана без спроса нельзя.

        :param path: путь к файлу; пусто — смотреть на экран.
        :param language: язык ответа.
        """
        code = "en" if str(language).startswith("en") else "ru"
        if path.strip():
            return await self._by_file(path, code)
        return await self._by_screen(code)

    @tool(
        phrases=[
            "открой место съёмки на карте",
            "покажи на карте где снято",
            "открой это место в картах",
            "show where this was taken on the map",
        ],
        reversible=False,
    )
    async def place_on_map(self, path: str = "", language: str = "ru") -> ToolResult:
        """Найти место съёмки и открыть его в картах.

        :param path: путь к файлу; пусто — смотреть на экран.
        :param language: язык ответа.
        """
        found = await self.photo_place(path=path, language=language)
        if not found.ok:
            return found
        url = (found.value or {}).get("map_url") if isinstance(found.value, dict) else None
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
        place = (found.value or {}).get("place") or ""
        return ToolResult.success(
            found.value,
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

    async def _by_screen(self, code: str) -> ToolResult:
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
        looked = await self.tools.invoke("screen.look", {"question": _ASK[code]})
        if not looked.ok:
            return looked
        return await self._answer(str(looked.value or ""), code, exact=None)

    async def _by_file(self, path: str, code: str) -> ToolResult:
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
            # Координаты в файле — редкая удача, зато точная: спрашиваем не «что
            # это за место», а «как называется вот эта точка».
            return await self._by_coordinates(exact, code, photo)

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
        answer = await self._ask_model(image, code)
        if answer is None:
            return ToolResult.failure(
                "зрячая модель не ответила",
                speech={
                    "ru": "Модель не ответила про эту фотографию.",
                    "en": "The model did not answer about this photo.",
                },
            )
        return await self._answer(answer, code, exact=None)

    async def _ask_model(self, image: str, code: str) -> str | None:
        """Показать картинку зрячей модели и получить название места."""
        messages = [Message.user(_ASK[code], images=(image,))]
        try:
            response = await self.context.llm.complete(messages, task=VISION_TASK)
        except Exception as exc:  # noqa: BLE001 — сеть и тариф, не наша вина
            self.log.warning("Зрячая модель не ответила: %s", exc)
            return None
        return response.text.strip()

    # --- что делаем с названием ---------------------------------------------

    async def _answer(self, said: str, code: str, *, exact: tuple[float, float] | None) -> ToolResult:
        """Собрать ответ по названию, которое дала модель."""
        if is_refusal(said):
            return ToolResult.failure(
                "модель места не узнала",
                speech={
                    "ru": "По этой фотографии место не узнаю.",
                    "en": "I cannot tell where this was taken.",
                },
            )
        place = clean_place(said)
        point = exact or await self._find(place)
        payload: dict[str, Any] = {
            "place": place,
            "exact": exact is not None,
            "latitude": point[0] if point else None,
            "longitude": point[1] if point else None,
            "map_url": map_url(*point) if point else "",
        }
        self.log.info("Место по фотографии: %r, точка %s", place, point)
        # «Похоже на» и «снято здесь» — разные утверждения, и путать их нельзя:
        # модель уверенно называет Анталию по любому южному пейзажу.
        if point is None:
            return ToolResult.success(
                payload,
                speech={
                    "ru": f"Похоже на {place}. На карте показать не смогу.",
                    "en": f"Looks like {place}. I cannot put it on the map though.",
                },
            )
        return ToolResult.success(
            payload,
            speech={
                "ru": f"Похоже на {place}.",
                "en": f"Looks like {place}.",
            },
        )

    async def _by_coordinates(
        self, point: tuple[float, float], code: str, photo: Path
    ) -> ToolResult:
        """Ответ по координатам из файла — единственный случай, когда мы знаем."""
        answer = await self._named(point)
        place = spoken_address(answer) if answer else ""
        payload = {
            "place": place,
            "exact": True,
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
                "ru": f"Снято здесь: {place}.",
                "en": f"Taken here: {place}.",
            },
        )

    # --- геокодер ------------------------------------------------------------

    async def _find(self, place: str) -> tuple[float, float] | None:
        """Название — в точку. ``None`` — геокодер такого не знает."""
        if not place:
            return None
        try:
            response = await self._http().get(
                f"{NOMINATIM}/search",
                params={"q": place, "format": "jsonv2", "limit": 1},
            )
            response.raise_for_status()
            found = response.json()
        except (httpx.HTTPError, ValueError) as error:
            self.log.warning("Геокодер не ответил на %r: %s", place, error)
            return None
        if not isinstance(found, list) or not found:
            return None
        try:
            return float(found[0]["lat"]), float(found[0]["lon"])
        except (KeyError, TypeError, ValueError):
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
