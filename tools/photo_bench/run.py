"""Замер скилла `photo_place` на наборе с известными координатами.

Отвечает на один вопрос и цифрой: **на сколько метров он промахивается**. До
этого замера скилл четырежды переделывали по одной фотографии, и каждый раз
казалось, что стало лучше.

Меряются две разные вещи, и путать их нельзя:

* **промах** — расстояние от ответа до настоящей точки съёмки;
* **честность** — уложился ли промах в ту точность, которую скилл сам обещал
  вслух. Ответ «Москва, только до города» с промахом в три километра честен, а
  «с точностью до здания» с тем же промахом — нет, и вреден именно этим.

Запуск:

    python tools/photo_bench/run.py --manifest bench/manifest.json --limit 20
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis.core.app import JarvisApp  # noqa: E402
from jarvis.core.config import load_config  # noqa: E402

USER_AGENT = (
    "JarvisPhotoBench/0.1 (https://github.com/mcdima0001/Jarvis; dev.iu.team@gmail.com)"
)

#: С какой точностью считаем попадание удачным. Владелец просил пятьсот метров.
TARGET_M = 500.0


def distance_m(first: tuple[float, float], second: tuple[float, float]) -> float:
    """Расстояние между точками по большому кругу, метров."""
    radius = 6_371_000.0
    lat1, lon1 = math.radians(first[0]), math.radians(first[1])
    lat2, lon2 = math.radians(second[0]), math.radians(second[1])
    d_lat, d_lon = lat2 - lat1, lon2 - lon1
    inner = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(inner)))


def cached(entry: dict[str, Any], folder: Path, client: httpx.Client) -> Path | None:
    """Скачать снимок, если его ещё нет. ``None`` — не вышло.

    Кеш обязателен: набор прогоняется десятки раз подряд, и качать одно и то же
    было бы и медленно, и невежливо по отношению к Commons.
    """
    name = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in entry["title"])
    photo = folder / f"{name[:120]}.jpg"
    if photo.exists() and photo.stat().st_size > 10_000:
        return photo
    try:
        response = client.get(entry["url"])
        response.raise_for_status()
    except httpx.HTTPError as error:
        print(f"  не скачался {entry['title']}: {error}", file=sys.stderr)
        return None
    photo.write_bytes(response.content)
    return photo


def strip_exif(photo: Path) -> None:
    """Убрать координаты из файла — иначе скилл прочитает ответ вместо работы.

    Это не формальность: Commons отдаёт снимки с GPS, а скилл первым делом
    смотрит в EXIF. Оставить его значило бы мерить не то, что мы измеряем.
    """
    from PIL import Image

    with Image.open(photo) as picture:
        clean = Image.new(picture.mode, picture.size)
        clean.putdata(list(picture.getdata()))
        clean.save(photo, format="JPEG", quality=92)


class Readings:
    """Что зрячая модель сказала про каждый снимок — один раз и навсегда.

    Замер распадается на две половины, и стоят они по-разному. **Чтение снимка
    стоит денег** и от правок в коде не меняется: модель видит ту же картинку и
    отвечает то же самое. **Поиск по карте бесплатен**, и правят как раз его —
    кластеры, пороги, границы области.

    Поэтому прочитанное кладётся в файл и переиспользуется. Иначе каждая проба
    новой пороговой константы стоила бы полного круга запросов к облаку, а
    сравнивать два прогона было бы нельзя: модель отвечает не слово в слово.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.said: dict[str, str] = {}
        self.fresh = 0
        if path and path.exists():
            self.said = json.loads(path.read_text(encoding="utf-8"))

    def wrap(self, skill: Any, title: str) -> None:
        """Подменить чтение снимка на запись из файла, если она есть."""
        original = getattr(skill, "_ask_model_original", None) or skill._ask_model
        skill._ask_model_original = original

        async def reading(image: str, code: str, hint: str) -> str | None:
            if title in self.said:
                return self.said[title]
            answer = await original(image, code, hint)
            if answer:
                self.said[title] = answer
                self.fresh += 1
                self.save()
            return answer

        skill._ask_model = reading

    def save(self) -> None:
        """Сохранить прочитанное. Пишем сразу: прогон часто обрывают на половине."""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.said, ensure_ascii=False, indent=2), encoding="utf-8"
        )


async def with_retry(call: Any, tries: int = 4) -> Any:
    """Повторить прогон, если ответ пришёл пустым по вине тарифа.

    У OpenRouter при нулевом балансе есть предел «в полёте»: крупный запрос с
    картинкой отвергается с кодом 402, если перед ним только что был другой.
    Скилл такой отказ глотает и честно отвечает «не узнаю», а замер от этого
    показывает провал механизма там, где механизма никто и не спрашивал.
    """
    for attempt in range(tries):
        result = await call()
        value = result.value if isinstance(result.value, dict) else {}
        if result.ok or value.get("latitude") is not None:
            return result
        if "деньги" in (result.error or ""):
            # Повторять нечего: пустой счёт от ожидания не наполнится.
            return result
        if attempt < tries - 1:
            await asyncio.sleep(6.0 * (attempt + 1))
    return result


async def measure(
    app: JarvisApp,
    entry: dict[str, Any],
    photo: Path,
    hint: str = "",
    readings: Readings | None = None,
) -> dict[str, Any]:
    """Прогнать один снимок и посчитать промах.

    Скилл зовётся **напрямую, мимо реестра**: у инструментов предел ожидания в
    тридцать секунд, и упёршийся в него прогон не дал бы ответа вовсе. Время всё
    равно меряется и печатается — в живой работе этот предел настоящий.
    """
    truth = (float(entry["latitude"]), float(entry["longitude"]))
    skill = app.skills.get("photo_place")
    if skill is None:
        raise RuntimeError("скилл photo_place не загружен")
    if readings is not None:
        readings.wrap(skill, entry["title"])
    started = time.perf_counter()
    try:
        result = await with_retry(
            lambda: skill.photo_place(path=str(photo), hint=hint)  # type: ignore[attr-defined]
        )
    except Exception as error:  # noqa: BLE001 — замер не должен падать на одном снимке
        return {"title": entry["title"], "city": entry["city"], "error": repr(error)}
    spent = time.perf_counter() - started
    value = result.value if isinstance(result.value, dict) else {}
    row: dict[str, Any] = {
        "title": entry["title"],
        "city": entry["city"],
        "ok": result.ok,
        "seconds": round(spent, 1),
        "place": value.get("place", ""),
        "promised_m": value.get("accuracy_m"),
        "checked": value.get("checked", False),
        "truth": truth,
    }
    lat, lon = value.get("latitude"), value.get("longitude")
    if result.ok and lat is not None and lon is not None:
        row["answer"] = (float(lat), float(lon))
        row["miss_m"] = round(distance_m(truth, (float(lat), float(lon))))
    else:
        row["answer"] = None
        row["miss_m"] = None
        row["reason"] = result.error or ""
    return row


def report(rows: list[dict[str, Any]]) -> None:
    """Напечатать итог: по каждому снимку и сводкой."""
    print(f"\n{'город':<16} {'промах':>9} {'обещано':>9}  {'сверен':<7} место")
    print("-" * 96)
    for row in sorted(rows, key=lambda item: (item.get("miss_m") is None, item.get("miss_m") or 0)):
        if row.get("error"):
            print(f"{row['city']:<16} {'СБОЙ':>9}            {row['error'][:50]}")
            continue
        miss = row.get("miss_m")
        miss_text = f"{miss / 1000:.1f} км" if miss is not None else "нет точки"
        promised = row.get("promised_m")
        promised_text = f"{promised / 1000:.1f} км" if promised else "—"
        mark = "да" if row.get("checked") else ""
        print(f"{row['city']:<16} {miss_text:>9} {promised_text:>9}  {mark:<7} {row.get('place', '')[:45]}")

    answered = [row for row in rows if row.get("miss_m") is not None]
    misses = sorted(row["miss_m"] for row in answered)
    honest = [
        row for row in answered
        if row.get("promised_m") and row["miss_m"] <= row["promised_m"]
    ]
    close = [row for row in answered if row["miss_m"] <= TARGET_M]
    print("-" * 96)
    print(f"снимков в наборе:        {len(rows)}")
    print(f"ответ с точкой:          {len(answered)}")
    if misses:
        middle = misses[len(misses) // 2]
        print(f"промах, медиана:         {middle / 1000:.1f} км")
        print(f"промах, лучший/худший:   {misses[0] / 1000:.1f} / {misses[-1] / 1000:.1f} км")
    print(f"ближе {TARGET_M:.0f} м:            {len(close)} из {len(rows)}")
    print(f"в пределах обещанного:   {len(honest)} из {len(answered)}")
    print(f"сверено со спутником:    {sum(1 for row in rows if row.get('checked'))}")
    seconds = [row["seconds"] for row in rows if row.get("seconds")]
    if seconds:
        print(f"время на снимок:         {sum(seconds) / len(seconds):.1f} с в среднем")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--photos", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--skip", type=int, default=0)
    parser.add_argument("--hint", default="")
    parser.add_argument("--readings", type=Path, default=None,
                        help="файл с прочитанным: заполняется сам, потом бесплатен")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    entries = json.loads(args.manifest.read_text(encoding="utf-8"))
    if args.skip:
        entries = entries[args.skip:]
    if args.limit:
        entries = entries[: args.limit]
    folder = args.photos or args.manifest.parent / "photos"
    folder.mkdir(parents=True, exist_ok=True)

    ready: list[tuple[dict[str, Any], Path]] = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60.0, follow_redirects=True) as client:
        for entry in entries:
            photo = cached(entry, folder, client)
            if photo is None:
                continue
            marker = folder / f"{photo.stem}.clean"
            if not marker.exists():
                with contextlib.suppress(Exception):
                    strip_exif(photo)
                marker.write_text("exif снят", encoding="utf-8")
            ready.append((entry, photo))
    print(f"готово снимков: {len(ready)}")

    os.environ.setdefault("JARVIS_QUIET", "1")
    readings = Readings(args.readings)
    app = JarvisApp.build(load_config())
    await app.start(ears=False, voice=False)
    rows: list[dict[str, Any]] = []
    try:
        for index, (entry, photo) in enumerate(ready, 1):
            print(f"[{index}/{len(ready)}] {entry['city']}: {entry['title'][:60]}")
            row = await measure(app, entry, photo, args.hint, readings)
            rows.append(row)
            miss = row.get("miss_m")
            print(f"    -> {row.get('place', '')[:50]!r}, промах "
                  f"{'?' if miss is None else f'{miss / 1000:.1f} км'}")
    finally:
        await app.stop()

    report(rows)
    if readings.path is not None:
        print(f"прочитано заново:        {readings.fresh}, всего в кеше {len(readings.said)}")
    if args.out:
        args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nподробности -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
