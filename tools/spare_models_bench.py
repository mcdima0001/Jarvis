r"""Какая бесплатная модель годится в запасные: разбор команд без денег.

Повод (24.09.2026): счёт OpenAI пуст, счёт OpenRouter тоже, а запасной
провайдер уже сделан — значит вопрос не «есть ли запасной», а «кем его
спрашивать». У OpenRouter два десятка бесплатных моделей с поддержкой
инструментов, и выбирать между ними надо замером, а не по названию.

    C:\Python314\python.exe tools/spare_models_bench.py
    C:\Python314\python.exe tools/spare_models_bench.py --models nvidia/nemotron-3.5-lightning:free

Стенд берёт живые реплики из логов вместе с тем, что по ним выбрала платная
модель, отдаёт их кандидату **с настоящим каталогом инструментов** и считает два
числа: сколько разобрано так же и сколько это заняло времени. Разбор команд —
самая частая задача (она срабатывает на каждой неузнанной фразе), поэтому и
меряем её, а не свободный разговор.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dataclasses import replace  # noqa: E402

from jarvis.core.app import JarvisApp  # noqa: E402
from jarvis.core.config import load_config  # noqa: E402
from jarvis.core.llm import Message  # noqa: E402
from tools.offline_bench import heard  # noqa: E402

#: Кого пробуем по умолчанию: бесплатные, с инструментами, из разных семейств.
CANDIDATES = (
    "nex-agi/nex-n2.5-mini:free",
    "nex-agi/nex-n2.5-pro:free",
    "nvidia/nemotron-3.5-lightning:free",
)

#: Сколько реплик брать: каждая — это запрос, а бесплатные модели считают
#: медленно и сердито.
HOW_MANY = 8


async def main() -> int:
    parser = argparse.ArgumentParser(description="Бесплатная модель в запасные")
    parser.add_argument("--models", nargs="*", default=list(CANDIDATES))
    parser.add_argument("--count", type=int, default=HOW_MANY)
    parser.add_argument("--logs", default=str(ROOT / "logs"))
    args = parser.parse_args()

    app = JarvisApp.build(load_config())
    await app.start(ears=False, voice=False)
    try:
        schemas = app.registry.catalog().function_schemas()
        # Берём то, что платная модель когда-то разобрала: это и есть эталон.
        asked: list[tuple[str, str]] = []
        for _, text, tool, resolver in heard(Path(args.logs)):
            if resolver == "llm" and tool not in ("core.chat", "core.help") and len(text) > 8:
                asked.append((text, tool))
        asked = asked[-args.count:]
        if not asked:
            print("В логах нет разборов моделью — не на чем проверять.")
            return 1

        print(f"Реплик для проверки: {len(asked)}, инструментов в каталоге: {len(schemas)}\n")
        for model in args.models:
            # Меняем модель **запасного**: основной провайдер и так мёртв, и
            # весь поток уходит через запасного — ровно так, как в жизни.
            registry = app.llm.profiles
            registry._profiles["intent"] = replace(registry.get("intent"), fallback_model=model)
            app.llm._blocked.clear()
            hits, spent, broke = 0, 0.0, ""
            for text, tool in asked:
                started = time.perf_counter()
                try:
                    answer = await app.llm.complete(
                        [Message.user(text)], task="intent", tools=schemas
                    )
                except Exception as exc:  # noqa: BLE001 — чужая служба, замер продолжается
                    broke = f"{type(exc).__name__}: {str(exc)[:80]}"
                    break
                spent += time.perf_counter() - started
                chosen = answer.tool_calls[0].name if answer.tool_calls else ""
                hits += chosen.replace("__", ".") == tool
            if broke:
                print(f"{model:46} не отвечает — {broke}")
                continue
            print(f"{model:46} попаданий {hits}/{len(asked)}, {spent / len(asked):.1f} с на фразу")
        return 0
    finally:
        await app.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
