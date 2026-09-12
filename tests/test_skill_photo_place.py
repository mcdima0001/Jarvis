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


# --- зацепки и версии --------------------------------------------------------

_ANSWER = """ЗАЦЕПКИ: турецкий язык на вывесках, силуэт гор Бейдаглары, жёлтые такси
1) Анталия, Турция | Antalya, Turkey | 36.8969, 30.7133
2) Пляж Коньяалты | Konyaalti Beach, Antalya | нет
3) Старый город Калеичи | Kaleici, Antalya | 36.88, 30.70"""


def test_clues_and_guesses_are_read() -> None:
    """Из ответа вынимаются и зацепки, и все три версии с их полями."""
    reading = place.parse_reading(_ANSWER)

    assert "жёлтые такси" in reading.clues
    assert [guess.name for guess in reading.guesses] == [
        "Анталия, Турция",
        "Пляж Коньяалты",
        "Старый город Калеичи",
    ]
    assert reading.guesses[0].point == (36.8969, 30.7133)
    assert reading.guesses[1].point is None, "«нет» — это не координаты"


def test_local_name_is_asked_first() -> None:
    """Геокодер спрашивается местным написанием раньше русского.

    Замер 12.09.2026: «пляж Конъяалты, Анталия» и «отель Rixos Downtown
    Antalya» по-русски не находятся вовсе, а по-английски находятся.
    """
    guess = place.Guess(name="Пляж Коньяалты", local="Konyaalti Beach")

    assert guess.queries == ("Konyaalti Beach", "Пляж Коньяалты")
    assert place.Guess(name="Тверь").queries == ("Тверь",)
    assert place.Guess(name="Тверь", local="Тверь").queries == ("Тверь",)


def test_more_than_three_guesses_are_cut() -> None:
    """Четвёртая версия — уже перебор, и каждая стоит запроса к чужому сервису."""
    many = "\n".join(f"{n}) Версия {n} | Guess {n} | нет" for n in range(1, 6))

    assert len(place.parse_reading(many).guesses) == place.MAX_CANDIDATES


def test_sloppy_format_still_gives_something() -> None:
    """Формат не соблюдён — но ответ есть, и терять его нельзя."""
    assert place.parse_reading("Это точно Анталия").guesses == ()
    assert place.clean_place("Это точно Анталия") == "Это точно Анталия"


def test_refusal_inside_a_guess_is_skipped() -> None:
    """«Не знаю» версией не считается, даже если стоит под номером."""
    reading = place.parse_reading("1) не знаю | unknown | нет\n2) Тверь | Tver | нет")

    assert [guess.name for guess in reading.guesses] == ["Тверь"]


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
    """Координаты берём только тогда, когда это правда координаты.

    Перепутанные местами или выдуманные числа уехали бы в океан молча.
    """
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
    пляжа и у Средиземного моря. По рамке метка неотличима от здания, по рангу —
    отличима сразу.
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


# --- какая версия побеждает --------------------------------------------------


def test_the_most_precise_guess_wins_not_the_first() -> None:
    """Вероятная и точная — разные вещи, и владельцу нужна точная.

    Модель ставит первой самую вероятную версию: «Анталия» вернее «отеля
    Rixos», а толку от неё меньше — город владелец и сам найдёт.
    """
    assert place.tighter(100.0, 25_000.0)
    assert not place.tighter(25_000.0, 100.0)
    assert place.tighter(100.0, None), "известная точность лучше неизвестной"
    assert not place.tighter(None, 25_000.0)


def test_prompt_demands_a_spot_not_a_city() -> None:
    """Подсказка обязана требовать точку: на этом скилл и переделывали."""
    russian = " ".join(place._ASK["ru"].split())
    english = " ".join(place._ASK["en"].split())

    assert "ТОЧКА, а не город" in russian
    assert "SPOT, not a city" in english
    assert "ЗАЦЕПКИ" in russian, "без зацепок модель отвечает страной"
