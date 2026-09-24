r"""Что ассистент говорит чаще всего и что из этого готово заранее.

Последний пункт плана «ИИ как дополнение» (`docs/offline.md`) — заготовки
реплик впрок: пока сеть есть, ответы пишутся заранее и синтезируются, а без сети
звучит готовое и мгновенное. Но **заготавливать наугад нельзя**: это и деньги на
модель, и место в кеше синтеза. Поэтому сперва замер по живым логам — что
ассистент произносит на самом деле.

    C:\Python314\python.exe tools/replies_bench.py
    C:\Python314\python.exe tools/replies_bench.py --since 24.09 --top 25

Считаются три вещи, и каждая отвечает на свой вопрос:

* **самые частые реплики** — их и стоит держать готовыми;
* **откуда звук**: из кеша (мгновенно) или синтезом (секунда-полторы) — видно,
  сколько людям приходится ждать сегодня;
* **что ушло в модель** (`core.chat`) — это то, чего без сети не будет вовсе, и
  именно для этого нужны заготовки.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.offline_bench import heard  # noqa: E402

#: Строки лога, по которым видно сказанное и то, как оно прозвучало.
SAID = re.compile(r"^(?P<day>\d\d\.\d\d)\.\d\d, .*?voice\.pipeline\s+Отвечаю: (?P<text>.+)$")
CACHED = re.compile(r"^(?P<day>\d\d\.\d\d)\.\d\d, .*?tts\.composite\s+Реплика из кеша: (?P<text>.+)$")
FRESH = re.compile(r"^(?P<day>\d\d\.\d\d)\.\d\d, .*?tts\.composite\s+Реплика потоком: первый звук через (?P<first>[\d.]+)")


def lines(logs: Path, since: str) -> list[str]:
    """Строки логов за нужные дни — по порядку."""
    found: list[str] = []
    for log in sorted(logs.glob("jarvis-*.log")):
        found.extend(log.read_text("utf-8", "replace").splitlines())
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="Что ассистент говорит чаще всего")
    parser.add_argument("--since", default="", help="с какого дня, ДД.ММ")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--logs", default=str(ROOT / "logs"))
    args = parser.parse_args()

    said: Counter[str] = Counter()
    cached = fresh = 0
    waits: list[float] = []
    for line in lines(Path(args.logs), args.since):
        for pattern, kind in ((SAID, "said"), (CACHED, "cached"), (FRESH, "fresh")):
            match = pattern.match(line)
            if match is None:
                continue
            if args.since and match.group("day") < args.since:
                break
            if kind == "said":
                said[" ".join(match.group("text").split())] += 1
            elif kind == "cached":
                cached += 1
            else:
                fresh += 1
                waits.append(float(match.group("first")))
            break

    spoken = sum(said.values())
    if not spoken:
        print("В логах нет произнесённых реплик — нечего считать.")
        return 1

    print(f"Произнесено реплик: {spoken}")
    print(f"  из кеша (мгновенно): {cached}")
    if fresh:
        waits.sort()
        middle = waits[len(waits) // 2]
        print(f"  свежим синтезом:     {fresh}, медиана ожидания первого звука {middle:.2f} с")

    print(f"\nСамые частые реплики (их и держать готовыми), топ-{args.top}:")
    for text, count in said.most_common(args.top):
        short = text if len(text) <= 64 else text[:61] + "…"
        print(f"  {count:3} × {short}")

    # Сколько реплик сочинила модель: без сети их не будет вовсе.
    chat = sum(1 for _, _, tool, _ in heard(Path(args.logs)) if tool == "core.chat")
    commands = sum(1 for _, _, tool, _ in heard(Path(args.logs)) if tool != "core.chat")
    print(f"\nУшло в свободный разговор: {chat} реплик против {commands} команд")
    print("Это и есть то, чего без сети не будет: заготовки нужны именно здесь.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
