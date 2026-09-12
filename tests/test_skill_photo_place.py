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


# --- лестница ответа ---------------------------------------------------------

_LADDER = """ЗАЦЕПКИ: турецкий текст на вывесках, номер машины на 07, кипарисы
СТРАНА: Турция
ГОРОД: Анталья
РАЙОН: Коньяалты
МЕСТО: Пляж Коньяалты
МЕСТНОЕ: Konyaalti Beach"""


def test_steps_go_from_specific_to_general() -> None:
    """Версии строятся от частного к общему: это порядок доверия модели."""
    reading = place.parse_reading(_LADDER)

    assert "кипарисы" in reading.clues
    assert [guess.name for guess in reading.guesses] == [
        "Пляж Коньяалты",
        "Коньяалты, Анталья",
        "Анталья, Турция",
    ]


def test_wider_name_is_appended_for_the_geocoder() -> None:
    """«Коньяалты» без города найдётся где угодно, с городом — где нужно."""
    reading = place.parse_reading(_LADDER)

    assert reading.guesses[0].local == "Konyaalti Beach, Анталья"
    assert place._with_city("Анталья", "Турция") == "Анталья, Турция"
    assert place._with_city("Анталья, Турция", "Турция") == "Анталья, Турция"
    assert place._with_city("", "Турция") == ""


@pytest.mark.parametrize("said", ["нет", "Нет", "no", "none", "-", "неизвестно"])
def test_empty_step_is_not_a_place(said: str) -> None:
    """Незаполненная ступень значит «не знаю», и выдумывать за модель нечего."""
    assert place.is_empty(said)


def test_empty_step_does_not_become_a_guess() -> None:
    """Живой прогон 12.09.2026: модель честно ответила «нет» на три ступени.

    Разбор принял «нет» за название, и в геокодер уехало «нет, Анталья».
    """
    answer = _LADDER.replace("РАЙОН: Коньяалты", "РАЙОН: нет")
    answer = answer.replace("МЕСТО: Пляж Коньяалты", "МЕСТО: нет")
    answer = answer.replace("МЕСТНОЕ: Konyaalti Beach", "МЕСТНОЕ: нет")

    assert [guess.name for guess in place.parse_reading(answer).guesses] == [
        "Анталья, Турция"
    ]


def test_empty_marker_is_matched_whole_not_by_prefix() -> None:
    """«No» началом совпало бы с Новосибирском, а «нет» — с Нетанией."""
    assert not place.is_empty("Novosibirsk")
    assert not place.is_empty("Нетания")
    assert not place.is_empty("Норвегия")


# --- выдуманный адрес --------------------------------------------------------


@pytest.mark.parametrize(
    "invented",
    [
        "Перекресток D400 и улицы 2500. Sk",
        "D400 and 2500. Sk. intersection",
        "пересечение улиц Ататюрка и Джумхуриет",
        "шоссе E87",
    ],
)
def test_invented_address_is_dropped(invented: str) -> None:
    """Выдуманный адрес вреднее честного города: он звучит точно и уводит далеко.

    Живой прогон 12.09.2026: по фотографии дороги под Анталией модель выдала
    «перекрёсток D400 и улицы 2500. Sk» с координатами, промахнулась на
    двенадцать километров и **не назвала город** — хотя сама же прочитала на
    вывеске «ANTALYA BÜYÜKŞEHİR BELEDİYESİ» и номер машины на 07.
    """
    answer = _LADDER.replace("МЕСТО: Пляж Коньяалты", f"МЕСТО: {invented}")

    names = [guess.name for guess in place.parse_reading(answer).guesses]
    assert invented not in names
    assert names[-1] == "Анталья, Турция", "честный город обязан остаться"


@pytest.mark.parametrize(
    "real", ["Пляж Коньяалты", "Эйфелева башня", "Antalya Expo Center", "Красная площадь"]
)
def test_real_landmark_survives(real: str) -> None:
    """Настоящее место фильтр не трогает: он ловит адреса, а не названия."""
    answer = _LADDER.replace("МЕСТО: Пляж Коньяалты", f"МЕСТО: {real}")

    assert place.parse_reading(answer).guesses[0].name == real


def test_country_alone_is_still_an_answer() -> None:
    """Узнал только страну — это тоже ответ, и он верен."""
    answer = """СТРАНА: Турция
ГОРОД: нет
РАЙОН: нет
МЕСТО: нет"""

    assert [g.name for g in place.parse_reading(answer).guesses] == ["Турция"]


def test_nothing_recognised_gives_nothing() -> None:
    """Все ступени пусты — версий нет, и придумывать нечего."""
    answer = """СТРАНА: нет
ГОРОД: нет
РАЙОН: нет
МЕСТО: нет"""

    assert place.parse_reading(answer).empty


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("36.8969, 30.7133", (36.8969, 30.7133)),
        ("-33.8688, 151.2093", (-33.8688, 151.2093)),
        ("55,75; 37,62", (55.75, 37.62)),
        ("нет", None),
        ("", None),
        ("возможно где-то там", None),
        ("999.0, 30.0", None),
    ],
)
def test_coordinates_are_read_or_refused(said: str, expected) -> None:
    """Координаты берём только тогда, когда это правда координаты."""
    assert place.parse_point(said) == expected


# --- измеренная точность -----------------------------------------------------


def test_extended_object_is_measured_by_its_box() -> None:
    """У линии и области рамка — это правда её размер."""
    square = {
        "osm_type": "relation",
        "place_rank": 25,
        "boundingbox": ["55.752", "55.755", "37.619", "37.624"],
    }

    metres = place.precision_of(square)
    assert metres is not None and 200 < metres < 600
    assert place.describe_precision(metres) == "с точностью до квартала"


def test_point_object_is_judged_by_its_rank() -> None:
    """У метки рамка всегда одиннадцать метров и не значит ничего.

    Замер 12.09.2026: одинаковые одиннадцать метров у отеля, у семикилометрового
    пляжа и у Средиземного моря.
    """
    hotel = {"osm_type": "node", "place_rank": 30, "boundingbox": ["36.8", "36.8", "30.7", "30.7"]}
    city = {"osm_type": "node", "place_rank": 16, "boundingbox": ["36.8", "36.8", "30.7", "30.7"]}
    sea = {"osm_type": "node", "place_rank": 2, "boundingbox": ["35.0", "35.0", "20.0", "20.0"]}

    assert place.describe_precision(place.precision_of(hotel)) == "с точностью до здания"
    assert place.describe_precision(place.precision_of(city)) == "только до города"
    assert place.describe_precision(place.precision_of(sea)) == "только до региона"


def test_big_city_is_still_a_city() -> None:
    """Анталия занимает тридцать пять километров и остаётся городом, не регионом."""
    antalya = {
        "osm_type": "relation",
        "place_rank": 16,
        "boundingbox": ["36.75", "37.07", "30.55", "30.95"],
    }

    assert place.describe_precision(place.precision_of(antalya)) == "только до города"


def test_unknown_precision_is_not_invented() -> None:
    """Судить нечем — молчим, а не выдумываем число."""
    assert place.precision_of({"osm_type": "node"}) is None
    assert place.describe_precision(None) == ""


def test_longitude_shrinks_towards_the_poles() -> None:
    """Градус долготы в Норвегии вдвое короче, чем на экваторе."""
    equator = place.span_metres(["0.0", "0.0", "0.0", "1.0"])
    north = place.span_metres(["60.0", "60.0", "0.0", "1.0"])

    assert equator is not None and north is not None
    assert north < equator * 0.6


def test_prompt_allows_an_honest_city() -> None:
    """Подсказка обязана разрешать общий ответ: иначе модель сочиняет адрес.

    На требовании «дай точку» модель выдумала перекрёсток и выбросила город,
    который сама же доказала вывеской и номером машины.
    """
    russian = " ".join(place._ASK["ru"].split())
    english = " ".join(place._ASK["en"].split())

    assert "хуже честного города" in russian
    assert "worse than an honest city" in english
    assert "ЗАЦЕПКИ" in russian, "без зацепок модель отвечает страной"
