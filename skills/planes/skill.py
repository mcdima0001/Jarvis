"""Самолёты рядом: «что за самолёт только что взлетел», «что за самолёт над нами».

Живой случай 25.09.2026: на «что за самолёт только что взлетел возле меня»
ассистент ответил болтовнёй — «скажите, в какую сторону он ушёл». Владелец:
«Флайтрадар в помощь». Сперва проверили, справится ли план сам (задача в
`tools/agent_bench`): разбор увёл просьбу в разговор, и помочь ему было нечем —
на карте Flightradar самолёты нарисованы, а не перечислены, и где «возле меня»,
план не знает. Поэтому скилл.

**Данные — лента Flightradar24**, та же, что рисует их карту: самолёты в
прямоугольнике с кодами рейса, типа, аэропортов, высотой и скоростью набора.
Подробности о рейсе у них за Cloudflare (403), поэтому названия аэропортов и
авиакомпаний берутся из справочника рядом со скиллом (`data.json`, собирает
`tools/planes_data.py` из OurAirports и OpenFlights). Ключа не нужно; лента
неофициальная и может однажды закрыться — тогда скилл честно скажет, что не
достучался.

**«Где я» спрашивается у `whereabouts.here`**, а не хранится здесь: место одно
на всю систему, им же пользуется погода.

**Что значит «только что взлетел».** Не на земле, вылетел из аэропорта рядом
(`near_airport_km`), ниже `max_climb_ft` и не снижается. Из таких — самый
низкий: он оторвался последним. Нет такого — честно говорим, что взлетающих
нет, и называем ближайший самолёт в небе: спрашивают обычно, услышав гул.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool
from jarvis.core.tts.normalize import plural_form

FEED = "https://data-cloud.flightradar24.com/zones/fcgi/feed.js"
#: Без обычного браузерного заголовка лента отвечает пустым.
AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
DATA = Path(__file__).with_name("data.json")
HERE_TOOL = "whereabouts.here"

FEET = 0.3048

#: Типы по коду ICAO → как их называют. Только частые: редкий тип звучит кодом.
AIRCRAFT = {
    "A19N": "Airbus A319neo", "A20N": "Airbus A320neo", "A21N": "Airbus A321neo",
    "A318": "Airbus A318", "A319": "Airbus A319", "A320": "Airbus A320", "A321": "Airbus A321",
    "A332": "Airbus A330", "A333": "Airbus A330", "A339": "Airbus A330neo", "A359": "Airbus A350",
    "A35K": "Airbus A350", "A388": "Airbus A380", "BCS1": "Airbus A220", "BCS3": "Airbus A220",
    "B737": "Boeing 737", "B738": "Boeing 737", "B739": "Boeing 737", "B38M": "Boeing 737 MAX",
    "B39M": "Boeing 737 MAX", "B744": "Boeing 747", "B748": "Boeing 747", "B752": "Boeing 757",
    "B763": "Boeing 767", "B772": "Boeing 777", "B77W": "Boeing 777", "B77L": "Boeing 777",
    "B788": "Boeing 787", "B789": "Boeing 787", "B78X": "Boeing 787",
    "E190": "Embraer 190", "E195": "Embraer 195", "E75L": "Embraer 175", "E290": "Embraer 190",
    "SU95": "Суперджет", "AT72": "ATR 72", "AT76": "ATR 72", "DH8D": "Dash 8",
    "CRJ9": "Bombardier CRJ", "CL35": "бизнес-джет Challenger", "C68A": "бизнес-джет Citation",
}

_SUFFIXES = re.compile(r"\s+(International\s+)?(Airport|Havalimanı|Havalimani|Aeropuerto)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Plane:
    """Один самолёт из ленты."""

    flight: str
    callsign: str
    airline: str
    model: str
    latitude: float
    longitude: float
    altitude_ft: int
    vertical_fpm: int
    on_ground: bool
    origin: str
    destination: str


def parse_feed(data: dict[str, Any]) -> list[Plane]:
    """Самолёты из ответа ленты. Служебные ключи (`full_count`, `version`) — не списки."""
    planes: list[Plane] = []
    for row in data.values():
        if not isinstance(row, list) or len(row) < 19:
            continue
        try:
            planes.append(
                Plane(
                    flight=str(row[13] or ""), callsign=str(row[16] or ""), airline=str(row[18] or ""),
                    model=str(row[8] or ""), latitude=float(row[1]), longitude=float(row[2]),
                    altitude_ft=int(row[4] or 0), vertical_fpm=int(row[15] or 0), on_ground=bool(row[14]),
                    origin=str(row[11] or ""), destination=str(row[12] or ""),
                )
            )
        except (TypeError, ValueError):
            continue
    return planes


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние по поверхности Земли."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def just_took_off(
    planes: list[Plane],
    *,
    here: tuple[float, float],
    airports: dict[str, list[Any]],
    near_airport_km: float = 50.0,
    max_climb_ft: int = 12000,
) -> Plane | None:
    """Кто оторвался последним из аэропорта рядом: самый низкий из набирающих."""
    lat, lon = here

    def from_near(plane: Plane) -> bool:
        port = airports.get(plane.origin)
        return bool(port) and distance_km(lat, lon, float(port[2]), float(port[3])) <= near_airport_km

    rising = [
        plane for plane in planes
        if not plane.on_ground and plane.altitude_ft <= max_climb_ft and plane.vertical_fpm >= 0 and from_near(plane)
    ]
    return min(rising, key=lambda plane: plane.altitude_ft, default=None)


def nearest(planes: list[Plane], *, here: tuple[float, float]) -> Plane | None:
    """Ближайший самолёт в небе."""
    flying = [plane for plane in planes if not plane.on_ground]
    return min(flying, key=lambda plane: distance_km(*here, plane.latitude, plane.longitude), default=None)


def airport_name(code: str, airports: dict[str, list[Any]]) -> str:
    """Город аэропорта, а для незнакомого — сам код."""
    port = airports.get(code)
    if not port:
        return code
    return str(port[0] or _SUFFIXES.sub("", str(port[1])) or code)


def spoken_flight(flight: str) -> str:
    """«TK2325» → «TK 2325»: буквы и число синтез читает раздельно."""
    return re.sub(r"^([A-Z0-9]{2}?[A-Z]?)(\d+)$", r"\1 \2", flight) if flight else ""


def describe(
    plane: Plane,
    *,
    here: tuple[float, float],
    airports: dict[str, list[Any]],
    airlines: dict[str, str],
    rising: bool,
    near_airport_km: float = 50.0,
) -> dict[str, str]:
    """Что сказать о самолёте: кто, откуда и куда, как высоко и далеко.

    Аэропорт вылета рядом не называется: «из Gaziemir» — это пригород, где
    стоит аэропорт Измира, и владельцу он ничего не скажет. Своё — «летит в …».
    """
    who = airlines.get(plane.airline, "")
    flight = spoken_flight(plane.flight or plane.callsign)
    model = AIRCRAFT.get(plane.model, plane.model)
    head_ru = ", ".join(part for part in (who, f"рейс {flight}" if flight else "", model) if part)
    head_en = ", ".join(part for part in (who, f"flight {flight}" if flight else "", model) if part)
    route_ru = route_en = ""
    port = airports.get(plane.origin)
    local = bool(port) and distance_km(*here, float(port[2]), float(port[3])) <= near_airport_km
    if local and plane.destination:
        destination = airport_name(plane.destination, airports)
        route_ru, route_en = f" — летит в {destination}", f" — bound for {destination}"
    elif plane.origin or plane.destination:
        origin = airport_name(plane.origin, airports)
        destination = airport_name(plane.destination, airports)
        route_ru = f" — из {origin} в {destination}" if plane.origin and plane.destination else (
            f" — в {destination}" if plane.destination else f" — из {origin}")
        route_en = f" — {origin} to {destination}" if plane.origin and plane.destination else ""
    metres = int(round(plane.altitude_ft * FEET, -2))
    kilometres = max(1, round(distance_km(*here, plane.latitude, plane.longitude)))
    height_ru = f"высота {metres} {plural_form(metres, ('метр', 'метра', 'метров'))}" if metres else "у самой земли"
    far_ru = f"{kilometres} {plural_form(kilometres, ('километр', 'километра', 'километров'))} от вас"
    climb_ru = ", набирает высоту" if rising and plane.vertical_fpm > 0 else ""
    return {
        "ru": f"{head_ru}{route_ru}. Сейчас {height_ru}{climb_ru}, {far_ru}.",
        "en": f"{head_en}{route_en}. Now at {metres} metres, {kilometres} km from you.",
    }


class PlanesSkill(Skill):
    """Какой самолёт взлетел рядом или летит над головой — по ленте Flightradar24."""

    meta = SkillMeta(
        name="planes",
        description="Самолёты рядом по Flightradar24: кто взлетел, кто над головой",
        version="0.1.0",
        spoken=("самолёт", "самолеты", "flightradar"),
    )

    async def on_setup(self) -> None:
        self._radius_km = float(self.context.setting("radius_km", 60))
        self._near_airport_km = float(self.context.setting("near_airport_km", 50))
        self._max_climb_ft = int(self.context.setting("max_climb_ft", 12000))
        self._timeout = float(self.context.setting("timeout", 10))
        self._client: httpx.AsyncClient | None = None
        try:
            data = json.loads(DATA.read_text("utf-8"))
        except (OSError, ValueError) as error:
            self.log.warning("Справочник %s не прочитался (%s) — буду называть коды", DATA.name, error)
            data = {}
        self._airports: dict[str, list[Any]] = data.get("airports", {})
        self._airlines: dict[str, str] = data.get("airlines", {})

    async def on_stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _here(self) -> tuple[float, float] | None:
        if not self.tools.has(HERE_TOOL):
            return None
        found = await self.tools.invoke(HERE_TOOL, {})
        if not found.ok or not isinstance(found.value, dict):
            return None
        return float(found.value["latitude"]), float(found.value["longitude"])

    async def _planes(self, here: tuple[float, float]) -> list[Plane]:
        lat, lon = here
        dlat = self._radius_km / 111.0
        dlon = dlat / max(0.2, math.cos(math.radians(lat)))
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, headers={"User-Agent": AGENT})
        response = await self._client.get(
            FEED,
            params={
                "bounds": f"{lat + dlat:.3f},{lat - dlat:.3f},{lon - dlon:.3f},{lon + dlon:.3f}",
                "faa": 1, "satellite": 1, "mlat": 1, "flarm": 1, "adsb": 1, "gnd": 0, "air": 1,
                "vehicles": 0, "estimated": 0, "maxage": 14400, "gliders": 0, "stats": 0,
            },
        )
        response.raise_for_status()
        return parse_feed(response.json())

    async def _look(self) -> tuple[tuple[float, float], list[Plane]] | ToolResult:
        here = await self._here()
        if here is None:
            return ToolResult.failure(
                "место владельца неизвестно",
                speech={"ru": "Не знаю, где вы, сэр, — искать не вокруг чего.", "en": "I don't know where you are, sir."},
            )
        try:
            planes = await self._planes(here)
        except (httpx.HTTPError, ValueError) as error:
            self.log.warning("Лента Flightradar не ответила: %s", error)
            return ToolResult.failure(
                f"Flightradar24 недоступен: {error}",
                speech={"ru": "Flightradar не отвечает, сэр.", "en": "Flightradar isn't answering, sir."},
            )
        self.log.info("Самолётов вокруг: %d", len(planes))
        return here, planes

    def _say(self, plane: Plane, here: tuple[float, float], *, rising: bool, prefix: dict[str, str]) -> ToolResult:
        speech = describe(
            plane, here=here, airports=self._airports, airlines=self._airlines, rising=rising,
            near_airport_km=self._near_airport_km,
        )
        return ToolResult.success(
            {"flight": plane.flight, "callsign": plane.callsign, "airline": self._airlines.get(plane.airline, plane.airline),
             "aircraft": plane.model, "from": plane.origin, "to": plane.destination,
             "altitude_m": round(plane.altitude_ft * FEET), "climbing": plane.vertical_fpm > 0},
            speech={language: prefix.get(language, "") + text for language, text in speech.items()},
        )

    @tool(
        phrases=[
            "что за самолёт взлетел", "что за самолёт только что взлетел",
            "что за самолёт взлетел возле меня", "что за самолёт только что взлетел возле меня",
            "какой самолёт взлетел", "какой самолёт только что взлетел", "кто сейчас взлетел",
            "what plane just took off",
        ],
        reversible=True,
    )
    async def took_off(self) -> ToolResult:
        """Какой самолёт только что взлетел рядом с владельцем: рейс, откуда и куда."""
        looked = await self._look()
        if isinstance(looked, ToolResult):
            return looked
        here, planes = looked
        plane = just_took_off(
            planes, here=here, airports=self._airports,
            near_airport_km=self._near_airport_km, max_climb_ft=self._max_climb_ft,
        )
        if plane is not None:
            return self._say(plane, here, rising=True, prefix={})
        other = nearest(planes, here=here)
        if other is None:
            return ToolResult.success(
                {"flight": ""},
                speech={"ru": "Рядом сейчас ни одного самолёта в небе, сэр.", "en": "No planes near you right now, sir."},
            )
        return self._say(
            other, here, rising=False,
            prefix={"ru": "Взлетающих рядом сейчас нет. Ближе всех — ", "en": "Nothing taking off nearby. Closest is "},
        )

    @tool(
        phrases=[
            "что за самолёт пролетает", "что за самолёт летит", "что за самолёт над головой",
            "что за самолёт над нами", "какой самолёт пролетает", "какой самолёт над нами",
            "что за самолёт пролетает надо мной", "what plane is overhead",
        ],
        reversible=True,
    )
    async def overhead(self) -> ToolResult:
        """Какой самолёт сейчас ближе всех к владельцу в небе."""
        looked = await self._look()
        if isinstance(looked, ToolResult):
            return looked
        here, planes = looked
        plane = nearest(planes, here=here)
        if plane is None:
            return ToolResult.success(
                {"flight": ""},
                speech={"ru": "Рядом сейчас ни одного самолёта в небе, сэр.", "en": "No planes near you right now, sir."},
            )
        return self._say(plane, here, rising=plane.vertical_fpm > 0, prefix={})

    async def health(self) -> HealthStatus:
        if not self._airports:
            return HealthStatus.degraded("нет справочника аэропортов: собери tools/planes_data.py")
        return HealthStatus.healthy(f"аэропортов {len(self._airports)}, авиакомпаний {len(self._airlines)}")
