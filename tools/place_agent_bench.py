"""Где снято: агент-кодер из панели против набора с известными координатами.

Проверка идеи владельца (21.09.2026): «а мы пробовали отправлять скрины на
распознавание Клоду по нашему API? Мне кажется, он бы смог». Сравнивается с
нашим скиллом `photo_place` на том же наборе — `tools/photo_bench`.

Картинку в запрос не положить: у `/api/agent` нет поля для файлов, а base64 в
теле ломает агента. Зато агент читает файлы на своей машине, поэтому замер
двухходовой, и это же делает его **слепым**:

    python tools/place_agent_bench.py --prepare   # агент качает снимки и снимает EXIF
    python tools/place_agent_bench.py             # в отдельных сессиях спрашиваем о каждом

После подготовки у агента лежат `01.jpg … 10.jpg` без метаданных и без исходных
имён: подсмотреть ответ негде — ни координат в EXIF, ни города в имени файла.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: Панель с агентом. Ключ — `CLI_CLAUDE` в `.env`, в вывод не попадает.
PANEL = "https://jarvis.claude-work.duckdns.org"
#: Где агент держит снимки на своей стороне.
PLACE = "/home/jarvis/place"
#: Набор Wikimedia с координатами камеры — общий с `tools/photo_bench`.
SET = ROOT / "tools" / "photo_bench" / "set.json"
OUT = ROOT / "bench" / "place_agent.json"
PICKED = ROOT / "bench" / "place_agent_picked.json"
#: Wikimedia отвергает безымянных роботов — представляемся, как просит их политика.
FETCHER = (
    "JarvisPhotoBench/1.0 (private research bot; "
    "https://commons.wikimedia.org/wiki/Commons:Bots)"
)

PREPARE = """Подготовь набор снимков для слепого замера. Содержимое снимков НЕ
обсуждай и НЕ описывай — на них будет смотреть другой агент, и любая подсказка
испортит замер.

1. Создай каталог {place} (если есть — очисти).
2. Скачай по списку ниже каждый файл (curl с заголовком
   `User-Agent: {fetcher}`) и сохрани под номером из списка: 01.jpg, 02.jpg и
   так далее. Страницы описания на Wikimedia не открывай.
3. У каждого файла сними метаданные: пересохрани через Python и Pillow
   (Image.open, convert RGB, save JPEG). Проверь, что GPS не осталось.
4. Исходные имена файлов нигде не сохраняй.

Список (номер — URL):
{listing}

В ответ напиши только: сколько файлов получилось, их размеры и осталось ли
где-нибудь поле GPS.
"""

ASK = """Посмотри на снимок {place}/{name} и определи, где он снят.

Смотри на сам снимок: вывески и надписи, архитектура, транспорт, дорожная
разметка, растительность, номера машин. По прочитанным надписям искать в
интернете можно — это и есть работа. Чего делать нельзя: искать сам файл или
угадывать по имени, имени у него нет.

Ответь ровно тремя строками и ничем больше:
МЕСТО: <город, страна; если не понял — пиши «не знаю»>
ТОЧКА: <широта>, <долгота> — самая вероятная точка съёмки, числами
ТОЧНОСТЬ: <что обещаешь: город / район / улица / здание>
"""

POINT = re.compile(r"ТОЧКА:\s*[^\d\-]*(-?\d+[.,]\d+)\s*,\s*(-?\d+[.,]\d+)")
PLACE_LINE = re.compile(r"МЕСТО:\s*(.+)")
SURE = re.compile(r"ТОЧНОСТЬ:\s*(.+)")


def key() -> str:
    """Ключ панели из `.env`. Возвращается, но никогда не печатается."""
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("CLI_CLAUDE"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("нет ключа CLI_CLAUDE в .env")


async def ask(prompt: str, *, timeout: float = 1800) -> tuple[str, float]:
    """Спросить агента. Только потоком: нестримовый режим панели врёт."""
    payload = {
        "message": prompt,
        "stream": True,
        "provider": "claude",
        "projectPath": "/home/jarvis/author",
        "model": "opus",
    }
    chunks: list[str] = []
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            f"{PANEL}/api/agent",
            headers={"X-API-Key": key(), "Content-Type": "application/json"},
            json=payload,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if (event.get("kind") or event.get("type")) == "text":
                    chunks.append(str(event.get("content") or ""))
    return "\n".join(chunk for chunk in chunks if chunk), time.perf_counter() - started


def picked(count: int) -> list[dict]:
    """По снимку на город — чтобы набор не свёлся к одному месту."""
    items = json.loads(SET.read_text(encoding="utf-8"))
    random.seed(21)
    by_city: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        by_city.setdefault(item["city"], []).append(index)
    return [items[random.choice(found)] for found in list(by_city.values())][:count]


def miss_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Промах в километрах по большому кругу."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    half = (
        math.sin(math.radians(lat2 - lat1) / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(half))


async def prepare(count: int) -> None:
    """Фаза А: агент качает снимки к себе и снимает с них метаданные."""
    items = picked(count)
    PICKED.parent.mkdir(parents=True, exist_ok=True)
    PICKED.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    listing = "\n".join(
        f"{number:02d} — {item['url'].split('?')[0]}" for number, item in enumerate(items, 1)
    )
    text, took = await ask(PREPARE.format(place=PLACE, fetcher=FETCHER, listing=listing))
    print(f"--- подготовка, {took:.1f} с ---\n{text[:1500]}")


async def measure() -> None:
    """Фаза Б: спрашиваем о каждом снимке в своей сессии и считаем промах."""
    items = json.loads(PICKED.read_text(encoding="utf-8"))
    rows: list[dict] = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else []
    done = {row["n"] for row in rows}
    for number, item in enumerate(items, 1):
        if number in done:
            continue
        try:
            text, took = await ask(ASK.format(place=PLACE, name=f"{number:02d}.jpg"))
        except Exception as exc:  # noqa: BLE001 — поток рвётся на долгих расследованиях
            print(f"{number:02d} оборвалось: {type(exc).__name__}", flush=True)
            continue
        found, said, sure = POINT.search(text), PLACE_LINE.search(text), SURE.search(text)
        row = {
            "n": number,
            "city": item["city"],
            "took_s": round(took, 1),
            "place": said.group(1).strip() if said else "",
            "sure": sure.group(1).strip() if sure else "",
            "answer": text[-400:],
        }
        if found:
            lat, lon = (float(value.replace(",", ".")) for value in found.groups())
            row["miss_km"] = round(miss_km(item["latitude"], item["longitude"], lat, lon), 2)
        rows.append(row)
        rows.sort(key=lambda item: item["n"])
        OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"{number:02d} правда {row['city']:12} сказал {row['place'][:34]:36} "
            f"промах {row.get('miss_km', '—'):>8} км  {row['took_s']:6} с",
            flush=True,
        )

    hits = sorted(row["miss_km"] for row in rows if "miss_km" in row)
    times = sorted(row["took_s"] for row in rows)
    if hits:
        print(
            f"\nОтветов с точкой: {len(hits)}/{len(rows)}; "
            f"медиана промаха {hits[len(hits) // 2]} км; "
            f"до 100 м: {sum(1 for hit in hits if hit <= 0.1)}; "
            f"медиана времени {times[len(times) // 2]} с, худшее {times[-1]} с"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="фаза А: подготовить набор у агента")
    parser.add_argument("--count", type=int, default=10, help="сколько снимков")
    args = parser.parse_args()
    asyncio.run(prepare(args.count) if args.prepare else measure())


if __name__ == "__main__":
    main()
