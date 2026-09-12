"""Причесать собранный набор: развести точки и перемешать города.

Две беды у сырого набора из Commons, и обе портят замер.

**Снимки липнут друг к другу.** Один фотограф выкладывает серию с одного места:
четыре кадра железной дороги в Москве, снятые с одной точки, — это не четыре
замера, а один, посчитанный четырежды. Разводим: два снимка ближе `APART`
метров считаются одним местом.

**Города идут подряд.** Замер часто обрывают на половине (кончились деньги,
надоело ждать), и набор, отсортированный по городам, оборвётся на Москве и
Петербурге. Перемешиваем с постоянным зерном: порядок случайный, но один и тот
же во всех прогонах, иначе замеры несравнимы.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path
from typing import Any

#: Насколько далеко должны стоять два снимка, чтобы считаться разными местами.
APART = 300.0


def distance_m(first: tuple[float, float], second: tuple[float, float]) -> float:
    """Расстояние между точками по большому кругу, метров."""
    radius = 6_371_000.0
    lat1, lon1 = math.radians(first[0]), math.radians(first[1])
    lat2, lon2 = math.radians(second[0]), math.radians(second[1])
    inner = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * radius * math.asin(min(1.0, math.sqrt(inner)))


def spread(rows: list[dict[str, Any]], apart: float = APART) -> list[dict[str, Any]]:
    """Оставить по одному снимку с места."""
    kept: list[dict[str, Any]] = []
    for row in rows:
        point = (float(row["latitude"]), float(row["longitude"]))
        if any(
            distance_m(point, (float(other["latitude"]), float(other["longitude"]))) < apart
            for other in kept
        ):
            continue
        kept.append(row)
    return kept


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--apart", type=float, default=APART)
    args = parser.parse_args()

    rows = json.loads(args.manifest.read_text(encoding="utf-8"))
    kept = spread(rows, args.apart)
    random.Random(args.seed).shuffle(kept)
    args.out.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"было {len(rows)}, осталось {len(kept)} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
