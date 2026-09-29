"""Собрать справочник для скилла `planes`: аэропорты и авиакомпании по кодам.

Лента Flightradar24 отдаёт только коды — «DLM → NCE», «TVS», — а подробности о
рейсе (названия) у них за Cloudflare и отвечают 403 (замер 25.09.2026). Вслух
коды не годятся, поэтому названия берутся из открытых наборов и кладутся рядом
со скиллом, как модель раскладки у `keys`:

* аэропорты — OurAirports (общественное достояние), только с кодом IATA и
  регулярными рейсами: город, название, координаты;
* авиакомпании — OpenFlights (ODbL), только действующие с кодом ICAO;
* русские названия городов — Викиданные (CC0): «какой город обслуживает
  аэропорт» (P931) и его подпись по-русски. Владелец 29.09.2026: «Новосибирск
  можно было по-русски сказать» — в OurAirports города только латиницей.

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
WIKIDATA = "https://query.wikidata.org/sparql"
#: Без понятного имени с адресом Викиданные отвечают 403.
AGENT = "JarvisPlanesData/0.1 (https://github.com/mcdima0001/Jarvis) httpx"
#: Сколько городов спрашивать одним запросом: все сразу не укладываются в минуту.
CHUNK = 800


def airports(text: str) -> dict[str, list[object]]:
    """IATA → [город, название, широта, долгота] для аэропортов с регулярными рейсами.

    Пятым полем `main` дописывает город по-русски, если он известен.
    """
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


def _sparql(client: httpx.Client, query: str) -> list[dict[str, dict[str, str]]]:
    response = client.post(WIKIDATA, data={"query": query, "format": "json"}, headers={"User-Agent": AGENT})
    rows: list[dict[str, dict[str, str]]] = response.raise_for_status().json()["results"]["bindings"]
    return rows


def _entity(row: dict[str, dict[str, str]], key: str) -> str:
    return row[key]["value"].rsplit("/", 1)[-1]


def russian_cities(client: httpx.Client) -> dict[str, list[tuple[str, str]]]:
    """IATA → [(подпись по-английски, по-русски)] городов, которые аэропорт обслуживает."""
    served: dict[str, list[str]] = {}
    for row in _sparql(client, "SELECT ?iata ?city WHERE { ?port wdt:P238 ?iata; wdt:P931 ?city. }"):
        served.setdefault(row["iata"]["value"], []).append(_entity(row, "city"))
    cities = sorted({city for listed in served.values() for city in listed})
    labels: dict[str, dict[str, str]] = {}
    for start in range(0, len(cities), CHUNK):
        values = " ".join(f"wd:{city}" for city in cities[start:start + CHUNK])
        for row in _sparql(
            client,
            f"SELECT ?city ?label WHERE {{ VALUES ?city {{ {values} }} ?city rdfs:label ?label. "
            'FILTER(LANG(?label) = "ru" || LANG(?label) = "en") }',
        ):
            labels.setdefault(_entity(row, "city"), {})[row["label"]["xml:lang"]] = row["label"]["value"]
    return {
        code: [(labels[city].get("en", ""), labels[city]["ru"]) for city in listed if "ru" in labels.get(city, {})]
        for code, listed in served.items()
    }


def pick_russian(municipality: str, options: list[tuple[str, str]]) -> str:
    """Русское имя города: того, чья английская подпись совпала с OurAirports, иначе первого.

    Аэропорт бывает приписан к нескольким местам («Даламан» и провинция «Мугла»);
    верное то, что называет и OurAirports.
    """
    for english, russian in options:
        if english and english.casefold() == municipality.casefold():
            return russian
    return options[0][1] if options else ""


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
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        ports = airports(client.get(AIRPORTS).raise_for_status().text)
        lines = airlines(client.get(AIRLINES).raise_for_status().text)
        russian = russian_cities(client)
    for code, port in ports.items():
        port.append(pick_russian(str(port[0]), russian.get(code, [])))
    OUT.write_text(
        json.dumps({"airports": ports, "airlines": lines}, ensure_ascii=False, separators=(",", ":")),
        "utf-8",
    )
    named = sum(1 for port in ports.values() if port[4])
    print(
        f"Аэропортов {len(ports)} (по-русски {named}), авиакомпаний {len(lines)}, "
        f"{OUT.stat().st_size // 1024} КБ: {OUT}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
