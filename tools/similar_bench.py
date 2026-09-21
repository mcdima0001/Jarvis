"""Стенд резолвера ослышек: что он поймал бы на живых репликах из логов.

Механизм, который нельзя измерить, не ставится. Здесь измеряется главное —
**ложные срабатывания**: реплика, которую роутер уже разобрал верно, не должна
после этого уезжать в чужую команду.

    python tools/similar_bench.py logs/jarvis-2026-09-*.log
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.app import JarvisApp  # noqa: E402
from jarvis.core.config import load_config  # noqa: E402
from jarvis.core.contracts import Utterance  # noqa: E402
from jarvis.core.router.resolvers.similar import SimilarResolver  # noqa: E402

#: «Реплика 'добавь басов' -> peace.more_bass (резолвер phrase, уверенность 1.00)»
LINE = re.compile(r"Реплика '(?P<text>[^']*)' -> (?P<tool>\S+) \(резолвер (?P<resolver>\w+)")


def said(paths: list[Path]) -> list[tuple[str, str, str]]:
    """Реплики из логов: текст, инструмент и кто разобрал."""
    seen: dict[str, tuple[str, str, str]] = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            found = LINE.search(line)
            if found:
                seen[found["text"].lower()] = (found["text"], found["tool"], found["resolver"])
    return list(seen.values())


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path)
    args = parser.parse_args()

    corpus = said(args.logs)
    if not corpus:
        print("В логах нет ни одной разобранной реплики")
        return 1

    app = JarvisApp.build(load_config())
    await app.start(ears=False, voice=False)
    resolver = SimilarResolver(app.registry)

    caught: list[tuple[str, str, str]] = []
    wrong: list[tuple[str, str, str]] = []
    for text, tool, by in corpus:
        intent = await resolver.resolve(Utterance(text=text, named=True))
        if intent is None:
            continue
        if by in ("phrase", "alias", "verbatim", "learned"):
            # Такие реплики до нас и не дойдут — но если мы предлагаем на них
            # другую команду, значит правило дырявое.
            if intent.tool != tool:
                wrong.append((text, tool, intent.tool))
        else:
            caught.append((text, tool, intent.tool))

    print(f"\nРеплик в логах: {len(corpus)}")
    print(f"Поймано бы вместо модели: {len(caught)}")
    for text, was, now in caught:
        mark = "=" if was == now else "≠"
        print(f"  {mark} {text!r}: было {was}, стало {now}")
    print(f"\nОпасных подмен разобранного: {len(wrong)}")
    for text, was, now in wrong:
        print(f"  ! {text!r}: {was} -> {now}")
    await app.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
