r"""Лишние слова внутри команды: что ловится и что подменяется.

Правило из `resolvers/loose.py` опасно ровно одним — слова команд живут и
внутри обычной речи. Поэтому оно меряется на живых репликах из `logs/`, а не на
придуманных примерах:

    C:\Python314\python.exe tools/loose_bench.py
    C:\Python314\python.exe tools/loose_bench.py --say "поставь в таймер на 5 минут"

Печатает две вещи, и вторая важнее первой: **находки** (реплика, которая без
правила ушла бы в облако) и **подмены** (реплика, которая раньше разбиралась
точной фразой, а теперь досталась другому инструменту). Подмен быть не должно
вовсе: пропущенную команду повторяют, а выполненную не туда — отменяют.

Замер 23.09.2026 на 306 репликах: одна находка (случай владельца), ноль подмен.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.core.app import JarvisApp  # noqa: E402
from jarvis.core.config import load_config  # noqa: E402
from jarvis.core.contracts import Utterance  # noqa: E402
from tools.offline_bench import heard  # noqa: E402

#: Резолверы, которых на замере не спрашивают: модель стоит денег, а «свободный
#: разговор» соглашается на что угодно.
WITHOUT = frozenset({"llm", "fallback"})

#: Что считается строгим разбором: если реплика разбиралась так, забрать её —
#: это подмена, а не находка.
STRICT = ("phrase", "alias", "verbatim", "plan")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Команды с лишними словами внутри")
    parser.add_argument("--say", action="append", default=[], help="проверить свою фразу")
    parser.add_argument("--logs", default=str(ROOT / "logs"))
    args = parser.parse_args()

    app = JarvisApp.build(load_config())
    await app.start(ears=False, voice=False)
    try:
        if args.say:
            for text in args.say:
                intent = await app.router.route(Utterance(text=text), without=WITHOUT)
                where = f"{intent.tool} {intent.arguments} [{intent.resolver}]" if intent else "—"
                print(f"{text!r} -> {where}")
            return 0

        said = heard(Path(args.logs))
        found: list[str] = []
        stolen: list[str] = []
        for _, text, tool, resolver in said:
            intent = await app.router.route(Utterance(text=text), without=WITHOUT)
            if intent is None or intent.resolver != "loose":
                continue
            line = f"  {text!r} -> {intent.tool} {intent.arguments}"
            if resolver in STRICT and intent.tool != tool:
                stolen.append(f"{line} (было {tool} через {resolver})")
            else:
                found.append(f"{line} (модель когда-то решила: {tool})")

        print(f"Реплик из лога: {len(said)}")
        print(f"\nНаходки — ушли бы в облако, а теперь разбираются даром: {len(found)}")
        print("\n".join(found))
        print(f"\nПодмены — этого быть не должно: {len(stolen)}")
        print("\n".join(stolen))
        return 1 if stolen else 0
    finally:
        await app.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
