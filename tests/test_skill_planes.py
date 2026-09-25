"""Самолёты рядом и «где я»: чистые функции, без сети.

Живой случай 25.09.2026: «что за самолёт только что взлетел возле меня» ушло в
болтовню. Первая живая проверка скилла нашла SunExpress XQ 886 из аэропорта
Измира на высоте 500 метров — тесты держат то, что там сработало.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load(folder: str) -> Any:
    path = _ROOT / "skills" / folder / "skill.py"
    spec = importlib.util.spec_from_file_location(f"{folder}_skill_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


planes = _load("planes")
whereabouts = _load("whereabouts")

IZMIR = (38.4127, 27.1384)
AIRPORTS = {
    "ADB": ["Gaziemir", "Adnan Menderes International Airport", 38.2924, 27.157],
    "DUB": ["Dublin", "Dublin Airport", 53.4213, -6.2701],
    "ATH": ["Spata-Artemida", "Athens International Airport", 37.9364, 23.9445],
    "PEK": ["Beijing", "Beijing Capital International Airport", 40.0801, 116.585],
}
AIRLINES = {"SXS": "SunExpress", "CCA": "Air China"}


def _row(*, flight: str, lat: float, lon: float, alt: int, vs: int, ground: int = 0,
         origin: str = "ADB", dest: str = "DUB", model: str = "B38M", airline: str = "SXS") -> list[object]:
    return ["4B", lat, lon, 90, alt, 200, "", "F", model, "TC-X", 0, origin, dest, flight, ground, vs, "CS" + flight, 0, airline]


def _feed() -> dict[str, object]:
    return {
        "full_count": 1234,
        "version": 4,
        "a": _row(flight="XQ886", lat=38.35, lon=27.16, alt=1600, vs=2200),
        "b": _row(flight="CA864", lat=38.45, lon=27.20, alt=34500, vs=0, origin="ATH", dest="PEK", model="A333", airline="CCA"),
        "c": _row(flight="XQ900", lat=38.30, lon=27.15, alt=4000, vs=1800),
        "d": _row(flight="TK2325", lat=38.29, lon=27.15, alt=0, vs=0, ground=1),
    }


def test_the_feed_skips_service_keys() -> None:
    parsed = planes.parse_feed(_feed())
    assert [plane.flight for plane in parsed] == ["XQ886", "CA864", "XQ900", "TK2325"]


def test_the_lowest_climbing_plane_from_a_near_airport_just_took_off() -> None:
    plane = planes.just_took_off(planes.parse_feed(_feed()), here=IZMIR, airports=AIRPORTS)
    assert plane is not None and plane.flight == "XQ886"


def test_a_cruising_plane_from_far_away_did_not_take_off_here() -> None:
    feed = {"b": _feed()["b"]}
    assert planes.just_took_off(planes.parse_feed(feed), here=IZMIR, airports=AIRPORTS) is None


def test_a_landing_plane_did_not_take_off() -> None:
    feed = {"x": _row(flight="PC100", lat=38.33, lon=27.16, alt=2000, vs=-800)}
    assert planes.just_took_off(planes.parse_feed(feed), here=IZMIR, airports=AIRPORTS) is None


def test_overhead_is_the_nearest_flying_plane() -> None:
    plane = planes.nearest(planes.parse_feed(_feed()), here=IZMIR)
    assert plane is not None and plane.flight == "CA864"


def test_a_local_departure_says_where_it_goes() -> None:
    plane = planes.parse_feed({"a": _feed()["a"]})[0]
    said = planes.describe(plane, here=IZMIR, airports=AIRPORTS, airlines=AIRLINES, rising=True)["ru"]
    assert said.startswith("SunExpress, рейс XQ 886, Boeing 737 MAX — летит в Dublin.")
    assert "Gaziemir" not in said
    assert "набирает высоту" in said
    assert "500 метров" in said


def test_a_far_flight_names_both_ends() -> None:
    plane = planes.parse_feed({"b": _feed()["b"]})[0]
    said = planes.describe(plane, here=IZMIR, airports=AIRPORTS, airlines=AIRLINES, rising=False)["ru"]
    assert "из Spata-Artemida в Beijing" in said
    assert "набирает" not in said


@pytest.mark.parametrize(("flight", "spoken"), [("XQ886", "XQ 886"), ("TK2325", "TK 2325"), ("U26124", "U2 6124"), ("", "")])
def test_flight_numbers_are_split_for_speech(flight: str, spoken: str) -> None:
    assert planes.spoken_flight(flight) == spoken


def test_place_from_settings_needs_both_coordinates() -> None:
    assert whereabouts.from_settings({"city": "Москва"}) is None
    assert whereabouts.from_settings(None) is None
    place = whereabouts.from_settings({"city": "Москва", "latitude": 55.75, "longitude": "37.61"})
    assert place is not None and place["longitude"] == 37.61 and place["source"] == "настройки"


def test_a_failed_lookup_is_not_a_place() -> None:
    assert whereabouts.from_lookup({"success": False, "message": "Reserved range"}) is None
    place = whereabouts.from_lookup({"success": True, "city": "Измир", "country": "Турция", "latitude": 38.41, "longitude": 27.13})
    assert place == {"city": "Измир", "country": "Турция", "latitude": 38.41, "longitude": 27.13, "source": "IP"}
