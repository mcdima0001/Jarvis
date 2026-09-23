"""Сколько ассистент понимает без сети: живые реплики из лога мимо модели.

Замысел владельца 21.09.2026 — «чтобы даже когда нет интернета было тяжело
определить, ИИ это или он». Догадками тут заниматься нечего: в `logs/` лежит
всё, что ему говорили месяцами. Стенд берёт оттуда реплики и прогоняет их через
**тот же роутер**, что и живой запуск, но с выключенной цепочкой к модели —
ровно то, что останется без сети или без денег на счету.

    C:\Python314\python.exe tools/offline_bench.py
    C:\Python314\python.exe tools/offline_bench.py --since 18.09 --show

Печатает долю разобранного и, по `--show`, список непонятого — это и есть
список кандидатов в `phrases` скиллов: каждая такая фраза стоит обращения к
модели при каждом повторе.

**Разговорные реплики считаются отдельно.** «Привет» и «как дела» роутер
отправляет в `core.chat`, то есть в облако; без сети они пропадают, но лечатся
они не шаблонами, а местным слоем вежливости, и мешать их с командами нельзя.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.core.app import JarvisApp  # noqa: E402
from jarvis.core.config import load_config  # noqa: E402
from jarvis.core.contracts import Utterance  # noqa: E402

#: Строка лога, которой роутер отчитывается о разборе.
LINE = re.compile(
    r"^(?P<day>\d\d\.\d\d)\.\d\d, \d\d:\d\d:\d\d .*?router\.router\s+Реплика "
    r"(?:'(?P<a>[^']*)'|\"(?P<b>[^\"]*)\") -> (?P<tool>\S+) \(резолвер (?P<res>\w+)"
)

#: Куда роутер отправляет то, что командой не является.
CHATTY = frozenset({"core.chat", "core.help"})


def heard(logs: Path, since: str = "") -> list[tuple[str, str, str, str]]:
    """Реплики из логов: день, что сказали, во что ушло, каким резолвером."""
    found: list[tuple[str, str, str, str]] = []
    for log in sorted(logs.glob("jarvis-*.log")):
        for line in log.read_text("utf-8", "replace").splitlines():
            match = LINE.match(line)
            if match is None:
                continue
            day = match.group("day")
            if since and day < since:
                continue
            text = match.group("a") if match.group("a") is not None else match.group("b")
            found.append((day, text, match.group("tool"), match.group("res")))
    return found


async def main() -> int:
    parser = argparse.ArgumentParser(description="Что понятно без сети")
    parser.add_argument("--since", default="", help="с какого дня, ДД.ММ")
    parser.add_argument("--logs", default=str(ROOT / "logs"))
    parser.add_argument("--show", action="store_true", help="показать непонятое")
    args = parser.parse_args()

    said = heard(Path(args.logs), args.since)
    if not said:
        print("В логах нет ни одной разобранной реплики — нечего мерить.")
        return 1

    app = JarvisApp.build(load_config())
    await app.start(ears=False, voice=False)
    try:
        # Модель выключаем целиком: без сети недоступна и она, и её отказ.
        без_модели = frozenset({"llm", "fallback"})
        counts: Counter[str] = Counter()
        lost: list[tuple[str, str]] = []
        for _, text, tool, _ in said:
            intent = await app.router.route(Utterance(text=text), without=без_модели)
            if intent is not None:
                counts[intent.resolver] += 1
            elif tool in CHATTY:
                counts["разговор"] += 1
            else:
                counts["потеряно"] += 1
                lost.append((text, tool))
    finally:
        await app.stop()

    total = len(said)
    print(f"\nРеплик из лога: {total}")
    for name, number in counts.most_common():
        print(f"  {name:10} {number:4}  {number * 100 // total:3}%")
    understood = total - counts["потеряно"] - counts["разговор"]
    print(f"\nПонято без сети: {understood} из {total} ({understood * 100 // total}%)")
    print(f"Ушло бы в разговор (тоже мимо, но лечится не шаблонами): {counts['разговор']}")

    if args.show and lost:
        print("\nНепонятое — кандидаты в phrases скиллов:")
        for text, tool in lost:
            print(f"  {text!r:60} модель когда-то решила: {tool}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
