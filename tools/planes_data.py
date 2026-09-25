"""Собрать справочник для скилла `planes`: аэропорты и авиакомпании по кодам.

Лента Flightradar24 отдаёт только коды — «DLM → NCE», «TVS», — а подробности о
рейсе (названия) у них за Cloudflare и отвечают 403 (замер 25.09.2026). Вслух
коды не годятся, поэтому названия берутся из открытых наборов и кладутся рядом
со скиллом, как модель раскладки у `keys`:

* аэропорты — OurAirports (общественное достояние), только с кодом IATA и
  регулярными рейсами: город, название, координаты;
* авиакомпании — OpenFlights (ODbL), только действующие с кодом ICAO.

    python tools/planes_data.py            # skills/planes/data.json
"""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "skills" / "planes" / "data.json"
AIRPORTS = "https://davidmegginson.github.io/ourairports-data/airports.csv"
AIRLINES = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airlines.dat"


def airports(text: str) -> dict[str, list[object]]:
    """IATA → [город, название, широта, долгота] для аэропортов с регулярными рейсами."""
    found: dict[str, list[object]] = {}
    for row in csv.DictReader(io.StringIO(text)):
        code = (row.get("iata_code") or "").strip()
        if len(code) != 3 or row.get("scheduled_service") != "yes":
            continue
        found[code] = [
            row.get("municipality") or "",
            row.get("name") or "",
            round(float(row["latitude_deg"]), 4),
            round(float(row["longitude_deg"]), 4),
        ]
    return found


def airlines(text: str) -> dict[str, str]:
    """ICAO → название для действующих авиакомпаний."""
    found: dict[str, str] = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 8:
            continue
        name, icao, active = row[1], row[4], row[7]
        if active == "Y" and len(icao) == 3 and icao.isalpha() and name and name != "\\N":
            found.setdefault(icao, name)
    return found


def main() -> int:
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        ports = airports(client.get(AIRPORTS).raise_for_status().text)
        lines = airlines(client.get(AIRLINES).raise_for_status().text)
    OUT.write_text(
        json.dumps({"airports": ports, "airlines": lines}, ensure_ascii=False, separators=(",", ":")),
        "utf-8",
    )
    print(f"Аэропортов {len(ports)}, авиакомпаний {len(lines)}, {OUT.stat().st_size // 1024} КБ: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
