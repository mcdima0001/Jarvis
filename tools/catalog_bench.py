r"""Сколько стоит каталог инструментов и можно ли отдавать модели не весь.

Идея пришла из статьи Яндекса про Alice AI (24.09.2026): они поставили перед
дорогой моделью маленькую, которая выбирает из документов только нужные куски, и
выиграли 40% пропускной способности без потери качества. У нас та же картина в
другом месте: на каждой неузнанной фразе в модель уезжает **весь каталог**, а
сам вопрос весит двадцать токенов.

    C:\Python314\python.exe tools/catalog_bench.py
    C:\Python314\python.exe tools/catalog_bench.py --limit 10 --show

Стенд отвечает на **первый** вопрос — тот, что решает судьбу идеи и ничего не
стоит: если отдавать модели не весь каталог, а короткий список, отобранный
местным сравнением, **останется ли в нём тот инструмент, который модель выбрала
на самом деле**? Разбор берётся из живых логов: там записано, что человек сказал
и что модель выбрала.

Второй вопрос — про деньги — этот стенд не решает, и врать про это не будет.
У OpenAI каталог кешируется (88–95% входа со второй фразы идёт вдесятеро
дешевле), а список, меняющийся от фразы к фразе, кеш ломает. Сравнивать надо
настоящими запросами и настоящим счётом, то есть при живом счёте OpenAI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.core.app import JarvisApp  # noqa: E402
from jarvis.core.config import load_config  # noqa: E402
from jarvis.core.text import rank  # noqa: E402
from tools.offline_bench import heard  # noqa: E402

#: Сколько символов приходится на токен в схемах инструментов. Не общее правило
#: «три символа на токен», а замер 11.09.2026: в JSON схем повторяются одни и те
#: же слова («type», «function», «properties»), и токенизатор жмёт их куда
#: сильнее обычного текста.
CHARS_PER_TOKEN = 10.4


def spellings(spec) -> list[str]:  # noqa: ANN001 — ToolSpec живёт в ядре
    """Чем инструмент похож на реплику: его фразы, имя и описание."""
    words = [spec.name.replace(".", " ").replace("_", " ")]
    words.extend(spec.phrases)
    if spec.description:
        words.append(spec.description)
    return words


async def main() -> int:
    parser = argparse.ArgumentParser(description="Цена каталога и короткий список")
    parser.add_argument("--limit", type=int, default=10, help="сколько инструментов в списке")
    parser.add_argument("--show", action="store_true", help="показать промахи")
    parser.add_argument("--logs", default=str(ROOT / "logs"))
    args = parser.parse_args()

    app = JarvisApp.build(load_config())
    await app.start(ears=False, voice=False)
    try:
        catalog = app.registry.catalog()
        schemas = catalog.function_schemas()
        raw = json.dumps(schemas, ensure_ascii=False)
        print(f"Инструментов в каталоге: {len(schemas)}")
        print(f"Символов JSON: {len(raw)}")
        print(f"Токенов, оценка: ~{len(raw) / CHARS_PER_TOKEN:.0f} на каждой неузнанной фразе")

        # Кого можно предложить модели: те же, что в каталоге.
        names = {
            schema.get("function", schema).get("name", "") for schema in schemas
        }
        candidates = {
            spec.name: spellings(spec)
            for spec in app.registry.specs()
            if spec.name.replace(".", "__") in names or spec.name in names
        }

        asked = [(text, tool) for _, text, tool, res in heard(Path(args.logs)) if res == "llm"]
        if not asked:
            print("\nВ логах нет разборов моделью — не на чем проверять.")
            return 1

        hits, misses = 0, []
        for text, tool in asked:
            short = rank(text, candidates, limit=args.limit)
            if tool in short:
                hits += 1
            else:
                misses.append((text, tool, short[:3]))

        total = len(asked)
        print(f"\nРазборов моделью в логах: {total}")
        print(f"Инструмент остался бы в списке из {args.limit}: {hits} ({hits * 100 // total}%)")
        print(f"Потерялся бы: {len(misses)}")
        saved = (len(raw) - len(raw) * args.limit / max(1, len(schemas))) / CHARS_PER_TOKEN
        print(f"Сэкономленных токенов на фразе (если бы не кеш): ~{saved:.0f}")

        if args.show and misses:
            print("\nПотерянные — их модель уже не увидела бы:")
            for text, tool, best in misses[:25]:
                print(f"  {text!r:52} нужен {tool}, а предложили бы {', '.join(best)}")
        return 0
    finally:
        await app.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
