"""Место съёмки по фотографии: узнавание по виду, координаты — если повезёт.

Сети тут нет, зрячей модели тоже: проверяется то, что решает код. Главное
проверяемое свойство — **догадка не выдаётся за замер**: координаты из файла
это «снято здесь», узнавание по виду — «похоже на».
"""

from __future__ import annotations

import importlib.util
import re
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


def test_direct_clues_are_named_in_the_prompt() -> None:
    """Номер машины и вывеска органа власти называют город прямо, а не намёком.

    Без этой строки модель дважды из трёх прогонов останавливалась на стране,
    хотя сама же прочитала на снимке «ANTALYA BÜYÜKŞEHİR BELEDİYESİ» и номер на
    07 (живые прогоны 12.09.2026).
    """
    russian = " ".join(place._ASK["ru"].split())

    assert "автомобильном номере" in russian
    assert "это не догадка" in russian


def test_signs_are_not_a_step_any_more() -> None:
    """Вывески как отдельная ступень откачены: они дают уверенный промах.

    Прогон 12.09.2026: модель прочитала на баннере «ANTALYA BÜYÜKŞEHİR
    BELEDİYESİ», геокодер нашёл по этому имени **здание муниципалитета** в
    центре города, и скилл пообещал «с точностью до здания», промахнувшись на
    десять километров. Вывеска называет организацию, а не то место, где стоишь:
    баннер, реклама и объявление о продаже висят где угодно.
    """
    assert "ВЫВЕСКИ" not in place._ASK["ru"]
    assert "signs" not in place._FIELDS


def test_specific_guess_must_lie_inside_the_wider_one() -> None:
    """Частное обязано лежать внутри общего: иначе имя уводит куда угодно."""
    city = {"boundingbox": ["36.75", "37.07", "30.55", "30.95"]}

    assert place._inside(city, (36.88, 30.70))
    assert place._inside(city, (36.74, 30.60)), "запас у границы обязан быть"
    assert not place._inside(city, (39.93, 32.86)), "другой город прошёл проверку"
    assert place._inside(None, (0.0, 0.0)), "нет области — верим на слово"


# --- сверка со спутником -----------------------------------------------------


def test_tile_numbers_match_the_standard_scheme() -> None:
    """Пересчёт точки в номер тайла — обычная схема карт, проверяем по месту."""
    x, y = place.tile_of(56.3433, 37.5197, 16)

    assert round(x) == 39598 and round(y) == 20295
    # На нулевом масштабе весь мир — один тайл, и центр приходится на середину.
    centre = place.tile_of(0.0, 0.0, 0)
    assert round(centre[0], 3) == 0.5 and round(centre[1], 3) == 0.5


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("СХОДСТВО: 10", 10),
        ("сходство: 0\nПОЧЕМУ: ничего", 0),
        ("MATCH: 7", 7),
        ("СХОДСТВО: 99", 10),
        ("ПОЧЕМУ: нет оценки", None),
        ("", None),
    ],
)
def test_match_score_is_read(said: str, expected) -> None:
    """Оценка сходства вынимается из ответа и держится в своих границах."""
    assert place.read_match(said) == expected


def test_missing_tile_does_not_cancel_the_check() -> None:
    """Один сбойный тайл не повод отказываться от сверки целиком."""
    import io as _io

    from PIL import Image

    piece = _io.BytesIO()
    Image.new("RGB", (256, 256), (10, 120, 10)).save(piece, format="PNG")
    tiles = {(0, 0): piece.getvalue(), (2, 2): piece.getvalue()}

    body = place.stitch(tiles, 3)

    with Image.open(_io.BytesIO(body)) as sewn:
        assert sewn.size == (768, 768)
        assert sewn.getpixel((10, 10))[1] > 80, "положенный тайл не встал на место"
        assert sewn.getpixel((384, 384)) == (128, 128, 128), "дырка должна быть серой"


# --- надписи со снимка: что вообще ищем на карте -----------------------------


def test_signs_are_kept_apart_from_guesses() -> None:
    """Надпись — списанный факт, версия — мнение, и держатся они врозь.

    Весь день 12.09.2026 скилл верил мнению модели о месте и четырежды подряд
    ошибался на километры. Вывеска же либо есть на снимке, либо нет.
    """
    reading = place.parse_reading(
        "ЗАЦЕПКИ: английский язык\n"
        "ТЕКСТЫ: THE GATEHOUSE; UPSTAIRS AT GATEHOUSE; BAR\n"
        "УЛИЦА: North Road\n"
        "ДОМ: 1\n"
        "ЗАВЕДЕНИЕ: The Gatehouse\n"
        "СТРАНА: Великобритания\n"
        "ГОРОД: Лондон\n"
        "МЕСТО: нет"
    )

    assert reading.named[0] == "North Road 1", "адрес с домом точнее всего"
    assert "The Gatehouse" in reading.named
    assert "BAR" not in reading.named, "три буквы найдутся в любом городе"
    assert [guess.name for guess in reading.guesses] == ["Лондон, Великобритания"]


def test_repeated_sign_is_written_once() -> None:
    """Повтор не множится.

    На японском снимке модель выписала одну вывеску сорок раз подряд и упёрлась
    в предел ответа, так и не дойдя до ступеней (замер 13.09.2026).
    """
    same = "; ".join(["目黒川桜まつり"] * 40)
    reading = place.parse_reading(f"ТЕКСТЫ: {same}\nГОРОД: Токио")

    assert reading.texts == ("目黒川桜まつり",)


def test_nothing_written_is_not_a_sign() -> None:
    """«Нет» в строке надписей — это отказ, а не название."""
    assert place.parse_reading("ТЕКСТЫ: нет\nГОРОД: Вена").texts == ()


# --- терпимое сравнение с картой ---------------------------------------------


def test_sign_and_map_spell_the_same_place_differently() -> None:
    """«UPSTAIRS AT GATEHOUSE» обязано находить «Upstairs at the Gatehouse».

    Замер 13.09.2026: с точным образцом место не нашлось вовсе, с терпимым —
    нашлось в двадцати метрах. Разница была в одном артикле.
    """
    rule = re.compile(place.loose("UPSTAIRS AT GATEHOUSE"), re.IGNORECASE)

    assert rule.search("Upstairs at the Gatehouse")
    assert not rule.search("The Gatehouse")


def test_generic_words_stay_in_the_pattern() -> None:
    """Родовое слово выбрасывать нельзя.

    «North Road» без слова «road» вырождается в «north» и находит
    Нортумберленд.
    """
    assert "road" in place.loose("North Road")
    assert "набережная" in place.loose("Бережковская набережная")


def test_too_short_a_sign_is_not_searched() -> None:
    """Короткий кусок найдётся где угодно и только засорит выбор."""
    assert place.loose("BAR") == ""
    assert place.loose("the") == ""


def test_query_cannot_be_broken_by_what_is_written_on_a_photo() -> None:
    """Название приходит из чужого текста на снимке — экранируем.

    Собирать запрос из прочитанного без оглядки — то же самое, что подставлять
    его в SQL.
    """
    written = 'Кафе "У Ани"\n];out;'

    query = place.overpass_query((written, "North Road"), (55.0, 37.0, 56.0, 38.0))

    assert query.count("out center tags") == 1, "второй `out` означал бы вставку"
    assert query.count('"') == 4, "кавычек ровно столько, сколько поставили мы"
    assert "\n" not in query


# --- сходятся ли надписи в одном месте ---------------------------------------


def _hit(name: str, lat: float, lon: float, kind: str, *clues: str) -> Any:
    """Объект на карте, найденный по названным надписям."""
    return place.Hit(name=name, point=(lat, lon), kind=kind, clues=tuple(clues))


def test_two_different_signs_in_one_spot_win() -> None:
    """Две разные надписи с одного снимка в одной точке — это не случайность.

    Замер 13.09.2026 по лондонскому снимку: паб «The Gatehouse» и театр
    «Upstairs at the Gatehouse» сошлись в четырёх метрах, и до настоящей точки
    съёмки оттуда сорок три метра.
    """
    found = place.places((
        _hit("The Gatehouse", 51.5714, -0.1500, "pub", "THE GATEHOUSE"),
        _hit("Upstairs at the Gatehouse", 51.5714, -0.1499, "theatre",
             "THE GATEHOUSE", "UPSTAIRS AT GATEHOUSE"),
        _hit("Gatehouse School", 51.53, -0.05, "school", "THE GATEHOUSE"),
    ))

    assert len(found[0].clues) == 2, "верное место должно идти первым"
    assert found[0].spread < 10
    assert abs(found[0].point[0] - 51.5714) < 0.001


def test_one_sign_in_many_names_is_not_agreement() -> None:
    """Одна надпись, откликнувшаяся на несколько имён, — всё ещё одна надпись.

    На московском снимке обрывок «1-й КУТУЗ» нашёл станцию, бильярдный клуб и
    автосалон в одном квартале. По именам это выглядело бы трёхкратным
    подтверждением, по надписям — однократным.
    """
    found = place.places((
        _hit("Кутузовская", 55.74, 37.53, "station", "1-й КУТУЗ"),
        _hit("Бильярдный клуб Кутузовский", 55.7401, 37.5301, "leisure", "1-й КУТУЗ"),
        _hit("Форд центр Кутузовский", 55.7402, 37.5302, "shop", "1-й КУТУЗ"),
    ))

    assert len(found[0].names) == 3
    assert len(found[0].clues) == 1, "подтверждать себя надпись не может"


def test_a_sign_scattered_over_the_city_cannot_lead() -> None:
    """Надпись, рассыпанная по всему городу, — свидетель, но не улика."""
    scattered = tuple(
        _hit("Аренда", 55.7 + step / 100, 37.5 + step / 100, "shop", "АРЕНДА")
        for step in range(place.TOO_COMMON + 1)
    )

    found = place.places(scattered)

    assert not any(spot.solid for spot in found)


def test_a_common_sign_still_confirms_a_rare_one() -> None:
    """Частая надпись негодна как улика, но годна как свидетель.

    «GATEHOUSE» в Лондоне нашлось два десятка раз. Выбросив её, верное место
    осталось бы с единственной надписью, то есть без подтверждения.
    """
    everywhere = tuple(
        _hit("Gatehouse", 51.4 + step / 50, -0.3 + step / 50, "", "THE GATEHOUSE")
        for step in range(place.TOO_COMMON + 1)
    )

    found = place.places((
        *everywhere,
        _hit("The Gatehouse", 51.5714, -0.1500, "pub", "THE GATEHOUSE"),
        _hit("Upstairs at the Gatehouse", 51.5714, -0.1499, "theatre",
             "THE GATEHOUSE", "UPSTAIRS AT GATEHOUSE"),
    ))

    assert found[0].solid and len(found[0].clues) == 2
    assert abs(found[0].point[0] - 51.5714) < 0.001


def test_a_street_is_not_a_building() -> None:
    """Совпадение с улицей говорит о районе, с кафе — о доме."""
    street = place.places((_hit("North Road", 51.57, -0.15, "secondary", "NORTH ROAD"),))
    pub = place.places((_hit("The Gatehouse", 51.57, -0.15, "pub", "THE GATEHOUSE"),))

    assert street[0].metres >= place.STREET
    assert pub[0].metres <= place.STREET / 2


def test_spot_is_never_promised_tighter_than_the_floor() -> None:
    """Надписи сошлись в точку, а снимал человек всё равно с другой стороны."""
    together = place.places((
        _hit("A", 51.5714, -0.1500, "pub", "A"),
        _hit("B", 51.5714, -0.1500, "cafe", "B"),
    ))

    assert together[0].metres == place.FLOOR


def test_overpass_answer_is_matched_to_the_signs_that_found_it() -> None:
    """Overpass ищет все имена разом и не говорит, какое сработало."""
    answer = {"elements": [
        {"lat": 51.5714, "lon": -0.15, "tags": {"name": "Upstairs at the Gatehouse",
                                                "amenity": "theatre"}},
        {"center": {"lat": 51.57, "lon": -0.149}, "tags": {"name": "North Road",
                                                           "highway": "secondary"}},
        {"lat": 51.5, "lon": -0.1, "tags": {}},
    ]}

    hits = place.read_hits(answer, ("THE GATEHOUSE", "North Road"))

    assert hits[0].clues == ("THE GATEHOUSE",)
    assert hits[1].clues == ("North Road",) and not hits[1].spot
    assert hits[2].clues == (), "безымянный объект ничего не подтверждает"


# --- область поиска ----------------------------------------------------------


def test_area_is_trimmed_so_the_search_can_finish() -> None:
    """На рамке провинции Overpass отвечает 504 и не отвечает вовсе.

    Замер 13.09.2026: «Анталья, Турция» — это полтораста километров по стороне,
    и поиск по ней срывался целиком.
    """
    huge = ["36.0", "37.5", "29.5", "32.5"]

    box = place.bounds(huge, (36.87, 30.81))

    assert box is not None
    south, west, north, east = box
    assert (north - south) * 111_320 <= place.MAX_AREA + 1, "рамка не ужалась"
    assert south <= 36.87 <= north and west <= 30.81 <= east, "точка выпала из рамки"


def test_small_area_is_left_as_it_is() -> None:
    """Город меньше предела резать незачем.

    Дмитров — двенадцать километров по стороне, и обрезать там нечего. Лондон,
    для сравнения, сорок пять, то есть под нож попадает и он: это не ошибка, а
    цена возможности вообще получить ответ.
    """
    dmitrov = ["56.30", "56.38", "37.46", "37.58"]

    box = place.bounds(dmitrov)

    assert box is not None
    assert [round(value, 2) for value in box] == [56.30, 37.46, 56.38, 37.58]


def test_broken_box_is_not_a_crash() -> None:
    """Геокодер может не дать рамки, и это не повод падать."""
    assert place.bounds(None) is None
    assert place.bounds(["юг", "север", "запад", "восток"]) is None


# --- опознание из нескольких мест --------------------------------------------


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("СНИМОК: 2\nСХОДСТВО: 8\nПОЧЕМУ: мост на месте", (2, 8)),
        ("IMAGE: 1\nMATCH: 10\nWHY: same square", (1, 10)),
        ("СНИМОК: нет\nСХОДСТВО: 0", (None, 0)),
        ("СНИМОК: 9\nСХОДСТВО: 7", (None, 7)),
        ("не понял вопрос", (None, None)),
    ],
)
def test_choice_is_read(said: str, expected: Any) -> None:
    """Номер вне списка читается как отказ.

    Назвавший девятый снимок из трёх ничего не опознал.
    """
    assert place.read_choice(said, 3) == expected


def test_lineup_asks_which_one_not_whether_it_looks_alike() -> None:
    """Вопрос намеренно другой, чем при сверке одного места.

    На «похоже ли» железнодорожный мост отвечает «да» в любом городе: так
    подтвердилось место за шестьсот километров от верного (замер 13.09.2026).
    """
    asked = place._LINEUP["ru"].format(count=3)

    assert "одно из них" in asked
    assert "может и не быть ни одного" in asked
    assert "СНИМОК:" in asked


# --- что говорится вслух -----------------------------------------------------


def test_agreement_is_said_out_loud() -> None:
    """«Похоже на» и «сошлись надписи» — разные обещания.

    Владелец по ним решает, ехать туда или проверять ещё раз.
    """
    agreed = place.Candidate(
        name="The Gatehouse", point=(51.57, -0.15), metres=80.0,
        source="вывеска", agreed=2,
    )

    said = place._speech(agreed, ", с точностью до здания")["ru"]

    assert "сошлись" in said.lower()
    assert "похоже" not in said.lower()


def test_a_bare_guess_is_said_as_a_guess() -> None:
    """Неподтверждённая версия обязана звучать догадкой."""
    bare = place.Candidate(
        name="Анталья", point=(36.88, 30.70), metres=25_000.0, source="версия"
    )

    assert place._speech(bare, "")["ru"].startswith("Похоже на")


class _Silent:
    """Журнал, который никуда не пишет: у скилла без контекста его нет."""

    def info(self, *args: object) -> None: ...
    def debug(self, *args: object) -> None: ...
    def warning(self, *args: object) -> None: ...
    def error(self, *args: object) -> None: ...


# --- пустой счёт: сбой, о котором надо сказать прямо --------------------------


async def test_empty_account_is_named_out_loud(tmp_path: Path) -> None:
    """Кончились деньги — так и говорим, а не «не узнаю это место».

    Ночью 13.09.2026 замер выдал шестнадцать «не узнаю» подряд, и выглядело это
    провалом механизма. На деле OpenRouter отвечал «можешь позволить себе 105
    токенов из запрошенных 300». Сбой, о котором ассистент говорит не своими
    словами, стоит часов поисков не там.
    """
    from jarvis.core.errors import LLMOutOfCredits

    photo = _photo(tmp_path / "вид.jpg")

    class _Broke(place.PhotoPlaceSkill):  # type: ignore[misc, valid-type]
        log = _Silent()

        async def _ask_model(self, image: str, code: str, hint: str) -> str | None:
            raise LLMOutOfCredits("На счету OpenRouter кончились деньги")

    skill = _Broke.__new__(_Broke)

    answer = await skill._by_file(str(photo), "ru", "")

    assert not answer.ok
    assert "деньги" in (answer.error or "")
    said = answer.speech_for("ru")
    assert "OpenRouter" in said and "Пополни" in said


async def test_other_failures_are_still_an_honest_shrug(tmp_path: Path) -> None:
    """Сеть отвалилась — это по-прежнему «модель не ответила», не про деньги."""
    photo = _photo(tmp_path / "вид.jpg")

    class _Mute(place.PhotoPlaceSkill):  # type: ignore[misc, valid-type]
        log = _Silent()

        async def _ask_model(self, image: str, code: str, hint: str) -> str | None:
            return None

    skill = _Mute.__new__(_Mute)

    answer = await skill._by_file(str(photo), "ru", "")

    assert not answer.ok
    assert "деньги" not in (answer.error or "")
