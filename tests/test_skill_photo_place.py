"""Место съёмки по фотографии: узнавание по виду, координаты — если повезёт.

Сети тут нет, зрячей модели тоже: проверяется то, что решает код. Главное
проверяемое свойство — **догадка не выдаётся за замер**: координаты из файла
это «снято здесь», узнавание по виду — «похоже на».
"""

from __future__ import annotations

import importlib.util
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load() -> Any:
    path = _ROOT / "skills" / "photo_place" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_photo_place", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


place = _load()
Image = pytest.importorskip("PIL.Image")
ExifTags = pytest.importorskip("PIL.ExifTags")


def _photo(path: Path, point: tuple[float, float] | None = None, size=(64, 48)) -> Path:
    """Снимок на диске, при желании с координатами в EXIF."""
    picture = Image.new("RGB", size, (20, 40, 60))
    if point is None:
        picture.save(path)
        return path

    def parts(value: float) -> tuple[Fraction, Fraction, Fraction]:
        value = abs(value)
        degrees = int(value)
        minutes = int((value - degrees) * 60)
        seconds = (value - degrees - minutes / 60) * 3600
        return Fraction(degrees, 1), Fraction(minutes, 1), Fraction(round(seconds * 100), 100)

    exif = Image.Exif()
    latitude, longitude = point
    exif[ExifTags.IFD.GPSInfo] = {
        1: "N" if latitude >= 0 else "S",
        2: parts(latitude),
        3: "E" if longitude >= 0 else "W",
        4: parts(longitude),
    }
    picture.save(path, exif=exif)
    return path


# --- EXIF: удача, а не опора -------------------------------------------------


def test_coordinates_are_read_when_they_are_there(tmp_path: Path) -> None:
    """Координаты из файла читаются точно, включая юг и запад."""
    north = _photo(tmp_path / "anta.jpg", (36.8969, 30.7133))
    south = _photo(tmp_path / "sydney.jpg", (-33.8688, -151.2093))

    assert place.coordinates_of(north) == (36.8969, 30.7133)
    assert place.coordinates_of(south) == (-33.8688, -151.2093)


def test_no_coordinates_is_the_normal_case(tmp_path: Path) -> None:
    """Снимка без координат не пугаемся: это обычное дело, а не сбой.

    Из сорока снимков на машине владельца GPS не оказалось ни у одного:
    мессенджеры вырезают его при отправке, у скриншота его нет по построению.
    """
    assert place.coordinates_of(_photo(tmp_path / "plain.jpg")) is None
    assert place.coordinates_of(tmp_path / "нет-такого.jpg") is None


def test_broken_file_is_not_a_crash(tmp_path: Path) -> None:
    """Битый файл — просто отсутствие координат."""
    junk = tmp_path / "junk.jpg"
    junk.write_bytes("это не картинка".encode("utf-8"))

    assert place.coordinates_of(junk) is None


# --- картинка для модели -----------------------------------------------------


def test_big_picture_is_shrunk_to_the_limit(tmp_path: Path) -> None:
    """Большое ужимаем до предела: дальше цена в токенах растёт, читаемость нет."""
    big = _photo(tmp_path / "big.jpg", size=(4000, 2000))

    uri, size = place.picture_for_model(big)

    assert max(size) == place.LIMIT
    assert uri.startswith("data:image/jpeg;base64,")


def test_small_picture_is_left_alone(tmp_path: Path) -> None:
    """Мелкое не трогаем: растягивать нечего, а портить есть что."""
    small = _photo(tmp_path / "small.jpg", size=(800, 600))

    _, size = place.picture_for_model(small)

    assert size == (800, 600)


# --- ответ модели ------------------------------------------------------------


@pytest.mark.parametrize(
    "said", ["не знаю", "Не знаю.", "не могу определить", "unknown", "I cannot tell", ""]
)
def test_refusal_is_recognised(said: str) -> None:
    """Отказ надо отличать от ответа.

    «Не знаю», отправленное в геокодер, вернёт какую-нибудь деревню Незнаево, и
    догадка превратится в уверенный ответ с точкой на карте.
    """
    assert place.is_refusal(said)


@pytest.mark.parametrize("said", ["Анталия, Турция", "Эйфелева башня", "Санкт-Петербург"])
def test_real_answer_is_not_a_refusal(said: str) -> None:
    """Настоящее название отказом не считается."""
    assert not place.is_refusal(said)


def test_name_is_cleaned_for_the_geocoder() -> None:
    """Обрамление снимается с обоих концов одним набором.

    По отдельности точка и кавычка спасали друг друга: «Анталия, Турция».
    теряло точку и оставляло кавычку, а в геокодер уходило с мусором.
    """
    assert place.clean_place("«Анталия, Турция».") == "Анталия, Турция"
    assert place.clean_place("  Эйфелева башня, Париж  ") == "Эйфелева башня, Париж"
    assert place.clean_place("Рим.\nЭто Италия") == "Рим"
    assert place.clean_place("") == ""


# --- что говорится вслух -----------------------------------------------------


def test_address_is_short_and_from_precise_to_general() -> None:
    """Вслух идут три части адреса, от точной к общей, а не весь адрес."""
    answer = {
        "address": {
            "road": "Adnan Menderes Bulvarı",
            "suburb": "Этилер",
            "city": "Анталья",
            "country": "Турция",
        }
    }

    assert place.spoken_address(answer) == "Adnan Menderes Bulvarı, Этилер, Анталья"


def test_address_falls_back_to_the_display_name() -> None:
    """Разбора по частям нет — берём начало общего названия."""
    answer = {"display_name": "Анталья, Турция, Средиземноморье, Земля"}

    assert place.spoken_address(answer) == "Анталья, Турция, Средиземноморье"


def test_nothing_to_say_is_an_empty_string() -> None:
    """Пустого ответа геокодера не боимся."""
    assert place.spoken_address({}) == ""


def test_map_link_points_at_the_spot() -> None:
    """Ссылка ведёт на точку, а не на город целиком."""
    url = place.map_url(36.8969, 30.7133)

    assert "mlat=36.896900" in url and "mlon=30.713300" in url


# --- файл вообще ли это ------------------------------------------------------


def test_only_pictures_are_accepted(tmp_path: Path) -> None:
    """Не картинку зрению не показываем."""
    text = tmp_path / "заметка.txt"
    text.write_text("привет", encoding="utf-8")
    picture = _photo(tmp_path / "снимок.jpg")

    assert place.resolve_photo(str(text)) is None
    assert place.resolve_photo(str(tmp_path / "нет.jpg")) is None
    assert place.resolve_photo("") is None
    assert place.resolve_photo(f'"{picture}"') == picture
