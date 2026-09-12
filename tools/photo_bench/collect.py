"""Собрать набор фотографий с известными координатами — Wikimedia Commons.

Скилл `photo_place` за день переделывался четырежды, и каждая переделка давала
уверенно неверный ответ, потому что **мерить было не на чем**: у владельца
координат в снимках нет ни у одного из 874 файлов, а две фотографии, которые он
прислал руками, — это не замер, а анекдот.

Commons решает ровно эту задачу: миллионы снимков, у части из них в EXIF стоит
GPS камеры. Берём **камеру, а не объект**: у снимка горы с двадцати километров
координата объекта увела бы замер в никуда, а GPS камеры отвечает на тот самый
вопрос, который задают скиллу, — «где я стоял».

**Истина читается из самого файла, а не из подписи на странице.** Подпись часто
округлена до сотых доли градуса — это километр, и на таком замере пятьсот метров
не измеришь вовсе. EXIF же точен до метров. Качать ради него целые файлы не
нужно: EXIF лежит в начале JPEG, и хватает первых ста килобайт, запрошенных
заголовком `Range`.

Набор собирается **не по достопримечательностям**. Точки берутся кольцом вокруг
центра города, в нескольких километрах от него: там обычные улицы, дворы и
дороги — то, что снимает владелец. Достопримечательности узнаются и так, и
набор из них хвалил бы скилл за то, чего он не умеет.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

API = "https://commons.wikimedia.org/w/api.php"

#: Представляться обязательно и в их формате: имя, ссылка, почта. Без этого
#: Commons отвечает 403 — проверено.
USER_AGENT = (
    "JarvisPhotoBench/0.1 (https://github.com/mcdima0001/Jarvis; dev.iu.team@gmail.com)"
)

#: Откуда набирать. Города вперемешку: родные владельцу, те, куда он ездит, и
#: чужие — чтобы набор не льстил ни одной стране.
SEEDS: tuple[tuple[str, float, float], ...] = (
    ("Москва", 55.7558, 37.6173),
    ("Петербург", 59.9343, 30.3351),
    ("Дмитров", 56.3439, 37.5202),
    ("Тверь", 56.8587, 35.9176),
    ("Нижний Новгород", 56.3269, 44.0059),
    ("Казань", 55.7963, 49.1088),
    ("Сочи", 43.5855, 39.7231),
    ("Калининград", 54.7104, 20.4522),
    ("Анталья", 36.8969, 30.7133),
    ("Стамбул", 41.0082, 28.9784),
    ("Тбилиси", 41.7151, 44.8271),
    ("Ереван", 40.1792, 44.4991),
    ("Алматы", 43.2220, 76.8512),
    ("Минск", 53.9006, 27.5590),
    ("Прага", 50.0755, 14.4378),
    ("Берлин", 52.5200, 13.4050),
    ("Париж", 48.8566, 2.3522),
    ("Рим", 41.9028, 12.4964),
    ("Барселона", 41.3851, 2.1734),
    ("Амстердам", 52.3676, 4.9041),
    ("Вена", 48.2082, 16.3738),
    ("Лиссабон", 38.7223, -9.1393),
    ("Хельсинки", 60.1699, 24.9384),
    ("Лондон", 51.5074, -0.1278),
    ("Нью-Йорк", 40.7128, -74.0060),
    ("Токио", 35.6762, 139.6503),
    ("Бангкок", 13.7563, 100.5018),
    ("Дубай", 25.2048, 55.2708),
    ("Каир", 30.0444, 31.2357),
    ("Кейптаун", -33.9249, 18.4241),
    ("Буэнос-Айрес", -34.6037, -58.3816),
    ("Сидней", -33.8688, 151.2093),
)

#: На каком удалении от центра искать, километров. Ближе — достопримечательности,
#: дальше — пустыри, где снимков нет вовсе.
RING = (3.0, 9.0)

#: Что отбрасываем по названию, не тратя ни байта: снятое с воздуха, схемы,
#: карты и всё, что снято не человеком с земли.
REJECT = (
    "aerial", "satellite", "map of", "diagram", "logo", "coat of arms", "flag of",
    "panorama", "360", "drone", "plan of", "seal of", "banknote", "stamp",
    "аэро", "карта", "схема", "герб", "флаг",
)


def offset(latitude: float, longitude: float, km: float, bearing: float) -> tuple[float, float]:
    """Точка в километрах и азимуте от данной. Плоская земля тут годится."""
    north = km / 111.32
    east = km / (111.32 * math.cos(math.radians(latitude)) or 1e-9)
    return (
        latitude + north * math.cos(math.radians(bearing)),
        longitude + east * math.sin(math.radians(bearing)),
    )


def gps_of(head: bytes) -> tuple[float, float] | None:
    """Координаты камеры из EXIF начала файла. ``None`` — их там нет."""
    import io

    from PIL import Image, ImageFile

    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        with Image.open(io.BytesIO(head)) as picture:
            gps = picture.getexif().get_ifd(34853)
    except Exception:  # noqa: BLE001 — обрезанный файл, битый EXIF: просто нет координат
        return None
    if not gps or 2 not in gps or 4 not in gps:
        return None
    try:
        latitude = _degrees(gps[2], gps.get(1))
        longitude = _degrees(gps[4], gps.get(3))
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    if abs(latitude) < 0.001 and abs(longitude) < 0.001:
        return None  # нули вместо координат — обычная беда битого EXIF
    return round(latitude, 6), round(longitude, 6)


def _degrees(parts: Any, reference: Any) -> float:
    """Градусы, минуты и секунды EXIF — в одно число со знаком."""
    degrees, minutes, seconds = (float(part) for part in parts)
    value = degrees + minutes / 60 + seconds / 3600
    return -value if str(reference).strip().upper() in ("S", "W") else value


def page_gps(page: dict[str, Any], digits: int = 5) -> tuple[float, float] | None:
    """Координата камеры из подписи на странице. ``None`` — её нет или она груба.

    Знаки после запятой тут и есть мера доверия: подпись «55.75, 37.61» — это
    сетка в километр, и мерить на ней пятисотметровую точность нельзя. Пять
    знаков (метр) ставит только тот, кто скопировал их из EXIF.
    """
    for spot in page.get("coordinates") or ():
        if not isinstance(spot, dict) or spot.get("type") != "camera":
            continue
        try:
            latitude, longitude = float(spot["lat"]), float(spot["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if min(_digits(latitude), _digits(longitude)) < digits:
            continue
        return round(latitude, 6), round(longitude, 6)
    return None


def _digits(value: float) -> int:
    """Сколько значащих знаков после запятой в числе."""
    text = f"{value:.8f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


def head_of(client: httpx.Client, url: str, size: int = 131_072) -> bytes | None:
    """Начало файла — столько, чтобы в него влез EXIF. ``None`` — не вышло."""
    try:
        response = client.get(url, headers={"Range": f"bytes=0-{size - 1}"})
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    return response.content


def is_photo(url: str) -> bool:
    """Фотография ли это по ссылке.

    Смотрим на путь, а не на строку целиком: Commons приписывает к ссылке
    хвост со своей статистикой (`?utm_source=...`), и проверка по концу строки
    отвергала **все** файлы подряд.
    """
    return urlparse(url).path.lower().endswith((".jpg", ".jpeg"))


def unwanted(title: str) -> bool:
    """Снято не с земли или вовсе не снято."""
    low = title.lower()
    return any(word in low for word in REJECT)


def search(
    client: httpx.Client, point: tuple[float, float], radius: int, limit: int
) -> list[dict[str, Any]]:
    """Файлы рядом с точкой: ссылка на оригинал и на уменьшенную копию."""
    response = client.get(API, params={
        "action": "query", "format": "json", "formatversion": "2",
        "generator": "geosearch", "ggsnamespace": "6",
        "ggscoord": f"{point[0]}|{point[1]}", "ggsradius": str(radius),
        "ggslimit": str(limit),
        "prop": "imageinfo|coordinates", "iiprop": "url|size",
        "iiurlwidth": "1600",
        "coprop": "type|dim", "colimit": "500",
    })
    response.raise_for_status()
    pages = response.json().get("query", {}).get("pages", [])
    return pages if isinstance(pages, list) else []


def candidates(pages: list[dict[str, Any]], seen: set[str]) -> list[dict[str, Any]]:
    """Отсеять то, что заведомо не годится, не тратя ни байта."""
    out: list[dict[str, Any]] = []
    for page in pages:
        title = str(page.get("title", ""))
        if title in seen or unwanted(title):
            continue
        info = (page.get("imageinfo") or [{}])[0]
        original = str(info.get("url") or "")
        if not original or not is_photo(original):
            continue
        seen.add(title)
        out.append({"title": title, "info": info, "page": page})
    return out


def with_gps(
    client: httpx.Client, found: list[dict[str, Any]], city: str, workers: int
) -> list[dict[str, Any]]:
    """Оставить те снимки, у которых в EXIF есть координаты камеры.

    Головы файлов качаются параллельно: на каждый кандидат уходит сто килобайт,
    а координаты есть примерно у одного из пяти — последовательно набор
    собирался бы часами.
    """
    def read(item: dict[str, Any]) -> dict[str, Any] | None:
        info = item["info"]
        head = head_of(client, str(info["url"]))
        gps = gps_of(head) if head is not None else None
        source = "exif"
        if gps is None:
            gps = page_gps(item["page"])
            source = "подпись"
        if gps is None:
            return None
        return {
            "title": item["title"],
            "city": city,
            "url": info.get("thumburl") or info["url"],
            "page": info.get("descriptionurl", ""),
            "latitude": gps[0],
            "longitude": gps[1],
            "source": source,
            "width": info.get("thumbwidth") or info.get("width"),
            "height": info.get("thumbheight") or info.get("height"),
        }

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [row for row in pool.map(read, found) if row is not None]


def collect(per_city: int, seed: int, workers: int) -> list[dict[str, Any]]:
    """Обойти города и собрать снимки с координатами камеры."""
    random.seed(seed)
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=40.0) as client:
        for city, latitude, longitude in SEEDS:
            got = 0
            for attempt in range(10):
                if got >= per_city:
                    break
                km = random.uniform(*RING)
                point = offset(latitude, longitude, km, random.uniform(0, 360))
                try:
                    pages = search(client, point, 5000, 120)
                except httpx.HTTPError as error:
                    print(f"  {city}: запрос не прошёл ({error})", file=sys.stderr)
                    continue
                rows = with_gps(client, candidates(pages, seen), city, workers)
                take = rows[: per_city - got]
                picked.extend(take)
                got += len(take)
                print(f"  {city}: {got}/{per_city} (круг {km:.1f} км, попытка {attempt + 1})",
                      flush=True)
    return picked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-city", type=int, default=3)
    parser.add_argument("--seed", type=int, default=12)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    found = collect(args.per_city, args.seed, args.workers)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nСобрано {len(found)} снимков с координатами -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
