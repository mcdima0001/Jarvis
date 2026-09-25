r"""Стенд «Джарвис как в фильме»: сколько просьб без своего скилла он доводит до конца.

Владелец 25.09.2026: «в фильме Джарвис может что угодно, а мы точечно
дописываем команды». Прежде чем строить агента с руками, меряем, что есть
сейчас, — иначе улучшение останется верой.

    C:\Python314\python.exe tools/agent_bench/run.py --route-only
    C:\Python314\python.exe tools/agent_bench/run.py --live
    C:\Python314\python.exe tools/agent_bench/run.py --live --only 3 7

**--route-only** ничего не выполняет: смотрит, куда разбор уводит просьбу, и
сверяет с `ideal` из `tasks.yaml`. Стоит копейки (одна фраза — один запрос
разбора с кешированным каталогом) и экран не трогает. Оценка грубая: выбрать
верный инструмент ещё не значит выполнить.

**--live** выполняет каждую просьбу тем же путём, что голос (`app.say`), ждёт и
проверяет **состояние машины**: заголовки окон, адрес активной вкладки. Отсюда
три исхода:

* **решено** — проверка прошла;
* **честный отказ** — ассистент сказал, что не смог, и правда не смог;
* **ложный успех** — сказал «готово», а на машине ничего нет. Худший исход,
  потому что его не слышно: живые случаи 24.09 — «Готово» вместо нажатия
  кнопки и «Черновиков нет» вместо перезагрузки модулей.

Живой прогон открывает окна и вкладки на экране — запускать, когда машиной не
пользуются. Ничего не отправляется и не удаляется: задачи только открывают.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

TASKS = Path(__file__).with_name("tasks.yaml")
#: Сколько ждать после команды, прежде чем смотреть на машину. Программы и
#: страницы открываются не мгновенно; меньше — и честный успех посчитается
#: провалом.
SETTLE_S = 4.0
#: Те, кто «отвечает» всегда: попадание сюда — это не выполнение, а разговор.
TALKERS = frozenset({"core.chat", "core.help", "fallback"})


@dataclass
class Outcome:
    """Итог одной задачи."""

    number: int
    said: str
    tool: str
    resolver: str = ""
    claimed: bool = False
    solved: bool | None = None
    seconds: float = 0.0
    tokens: int = 0
    reply: str = ""

    @property
    def verdict(self) -> str:
        if self.solved is None:
            return "—"
        if self.solved:
            return "решено"
        return "ЛОЖНЫЙ УСПЕХ" if self.claimed else "честный отказ"


def load_tasks(path: Path = TASKS) -> list[dict[str, Any]]:
    """Задачи из YAML. Без проверки задача бессмысленна — такие отбрасываются."""
    raw = yaml.safe_load(path.read_text("utf-8")) or {}
    return [task for task in raw.get("tasks", []) if task.get("say") and task.get("check")]


def visible_titles() -> list[str]:
    """Заголовки всех видимых окон верхнего уровня."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
    titles: list[str] = []
    walker = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @walker
    def keep(handle: int, _: int) -> bool:
        if user32.IsWindowVisible(handle):
            buffer = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(handle, buffer, 512)
            if buffer.value:
                titles.append(buffer.value)
        return True

    user32.EnumWindows(keep, 0)
    return titles


def matches(check: dict[str, Any], *, titles: list[str], url: str) -> bool:
    """Прошла ли проверка состояния. Чистая функция — её проверяют тесты."""
    wanted_titles = [str(item).lower() for item in check.get("title", [])]
    wanted_urls = [str(item).lower() for item in check.get("url", [])]
    if wanted_titles and any(want in title.lower() for want in wanted_titles for title in titles):
        return True
    return bool(wanted_urls and url and any(want in url.lower() for want in wanted_urls))


async def active_url(app: Any) -> str:
    """Адрес активной вкладки через расширение. Нет расширения — пусто."""
    if not app.registry.has("browser.page_target"):
        return ""
    found = await app.registry.invoke("browser.page_target", {"active": True})
    if not found.ok or not isinstance(found.value, dict):
        return ""
    return str(found.value.get("url", ""))


async def route_only(app: Any, tasks: list[dict[str, Any]]) -> list[Outcome]:
    from jarvis.core.contracts import Utterance

    results: list[Outcome] = []
    for number, task in enumerate(tasks, start=1):
        before = app.llm.spending.total_tokens
        started = time.monotonic()
        intent = await app.router.route(Utterance(text=task["say"], source="text"))
        outcome = Outcome(
            number=number, said=task["say"],
            tool=intent.tool if intent else "—", resolver=intent.resolver if intent else "",
            seconds=time.monotonic() - started,
            tokens=app.llm.spending.total_tokens - before,
        )
        ideal = set(task.get("ideal", []))
        outcome.solved = outcome.tool in ideal if outcome.tool not in TALKERS else False
        results.append(outcome)
        print(f"{number:2}. {outcome.tool:28} ({outcome.resolver or '—':8}) {task['say']}")
    return results


async def live(app: Any, tasks: list[dict[str, Any]]) -> list[Outcome]:
    results: list[Outcome] = []
    for number, task in enumerate(tasks, start=1):
        before = app.llm.spending.total_tokens
        started = time.monotonic()
        result = await app.say(task["say"])
        spent = time.monotonic() - started
        await asyncio.sleep(SETTLE_S)
        solved = matches(task["check"], titles=visible_titles(), url=await active_url(app))
        outcome = Outcome(
            number=number, said=task["say"], tool=result.tool or "—",
            claimed=bool(result.ok) and (result.tool or "") not in TALKERS,
            solved=solved, seconds=spent,
            tokens=app.llm.spending.total_tokens - before,
            reply=(app.pipeline.last_reply or "")[:70],
        )
        results.append(outcome)
        print(f"{number:2}. {outcome.verdict:14} {outcome.tool:26} {spent:5.1f} с  {task['say']}")
        if outcome.reply:
            print(f"    ответ: {outcome.reply}")
    return results


def report(results: list[Outcome], *, mode: str) -> None:
    total = len(results)
    tokens = sum(item.tokens for item in results)
    print()
    if mode == "route":
        fitting = sum(1 for item in results if item.solved)
        talk = sum(1 for item in results if item.tool in TALKERS)
        print(f"Разбор увёл в подходящий инструмент: {fitting} из {total}")
        print(f"Ушло в разговор вместо дела:        {talk} из {total}")
    else:
        solved = sum(1 for item in results if item.solved)
        lying = sum(1 for item in results if item.solved is False and item.claimed)
        honest = sum(1 for item in results if item.solved is False and not item.claimed)
        print(f"Решено:          {solved} из {total}")
        print(f"Ложный успех:    {lying} из {total}   <- худший исход, его не слышно")
        print(f"Честный отказ:   {honest} из {total}")
        seconds = sorted(item.seconds for item in results)
        if seconds:
            print(f"Время ответа:    медиана {seconds[len(seconds) // 2]:.1f} с, худшее {seconds[-1]:.1f} с")
    print(f"Токенов на весь прогон: {tokens}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Стенд: просьбы без своего скилла")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--route-only", action="store_true", help="только разбор, ничего не выполнять")
    mode.add_argument("--live", action="store_true", help="выполнять и проверять состояние машины")
    parser.add_argument("--only", type=int, nargs="*", help="номера задач, с единицы")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    from jarvis.core.app import JarvisApp
    from jarvis.core.config import load_config

    tasks = load_tasks()
    if args.only:
        tasks = [task for number, task in enumerate(tasks, start=1) if number in set(args.only)]
    app = JarvisApp.build(load_config())
    await app.start(ears=False, voice=False)
    try:
        if args.route_only:
            report(await route_only(app, tasks), mode="route")
        else:
            report(await live(app, tasks), mode="live")
    finally:
        await app.stop("стенд закончен")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
