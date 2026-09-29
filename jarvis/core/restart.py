"""Перезапуск: по просьбе, с тем, что стоит пережить, и сам — когда ядро обновилось.

Просьба владельца 29.09.2026: «перезагружать ядро, не перезапуская Джарвиса».
Перезагрузку ядра внутри процесса разобрали и отвергли: Python подменяет модули,
но не созданные из них объекты, скиллы держат ссылки на классы ядра, потоки
хука и трея продолжают старый код — половина системы на новом, половина на
старом, и строка версии в логе врёт. А выигрыш — секунды: перезапуск целиком
замерен в 14 с от команды до «Слушаю». Поэтому сделано то, что на деле мешало:

* **просьба голосом** — `Lifecycle.restart`, её зовёт `core.restart`;
* **состояние переживает перезапуск** — `Carryover`: режимы, последние реплики
  разговора, заданный вопрос; о брошенных поручениях ассистент говорит сам;
* **перезапуск сам, когда ядро обновилось** — `CoreWatch`: файлы ядра на диске
  не те, с которыми запускались, владелец молчит, ничего не играет;
* **скилл обновился — переподключается сам** (тот же `CoreWatch`): перезапуск
  ради него не нужен, хватает `SkillManager.reload`, и короткое «на два модуля
  пришли обновления, обновил» (просьба владельца 29.09.2026).

**Перед любым перезапуском новый код собирается пробно** (`build_check`, 2 с).
Упавший при старте Jarvis из трея не поднимется сам, а голосом его уже не
позовёшь: перезапуск в сломанный код хуже, чем никакого.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.core.attention import LOW, NORMAL
from jarvis.core.contracts import AnnouncementRequested, Intent, SkillLoaded
from jarvis.core.pending import Pending
from jarvis.core.state import BRIEF, DEAF, QUIET
from jarvis.core.tts.normalize import plural_form

if TYPE_CHECKING:
    from jarvis.core.attention import Announcer
    from jarvis.core.bus import EventBus
    from jarvis.core.config.schema import RestartConfig
    from jarvis.core.dialogue import Conversation
    from jarvis.core.jobs import Jobs
    from jarvis.core.router.dispatcher import Dispatcher
    from jarvis.core.skills import SkillManager
    from jarvis.core.skills.discovery import SkillCandidate
    from jarvis.core.state import Modes
    from jarvis.core.tools import ToolRegistry

logger = logging.getLogger(__name__)

#: Какие файлы ядра считать кодом: изменение остального перезапуска не требует.
WATCHED = frozenset({".py", ".html", ".js", ".css"})

#: Режимы, которые переживают перезапуск. Только флаги: тихий режим (`QUIET`)
#: ещё и закрывает панель и отпускает модели, и флаг без этих действий врал бы.
CARRIED_MODES = (DEAF, BRIEF)

#: Что собирается пробно: конфиг и вся сборка системы, без звука и скиллов.
#: Скилл упасть может — он загружается отдельно и никого за собой не тянет;
#: а сломанное ядро не даст подняться ничему.
_BUILD = (
    "import sys; from jarvis.core.config import load_config; "
    "from jarvis.core.app import JarvisApp; JarvisApp.build(load_config(sys.argv[1]))"
)
BUILD_TIMEOUT_S = 90.0

#: Что сказать о брошенных при перезапуске поручениях.
_UNFINISHED = {
    "ru": "Перед перезапуском не доделал: {titles}. Повторите, если ещё нужно.",
    "en": "I didn't finish before restarting: {titles}. Ask again if you still need it.",
}
_TELL = (
    "Ядро обновилось, {address}. Скажите «перезапустись», когда будет удобно."
)

#: Что в папке скилла считается им самим: код и его настройки. Данные, которые
#: скилл пишет себе сам, сюда не входят — иначе он переподключал бы себя по кругу.
SKILL_FILES = frozenset({".py", ".yaml"})
#: Файлы скилла не менялись столько секунд — правка закончена.
SKILL_SETTLE_S = 20.0
#: Столько секунд без разговора — и скилл можно переподключить: это не
#: перезапуск, глухоты нет, но команду, идущую через него, рвать нельзя.
SKILL_IDLE_S = 20.0

#: Пробный импорт скилла в отдельном процессе: сломанный код не выгружает рабочий.
_IMPORT_SKILL = (
    "import sys; from pathlib import Path; "
    "from jarvis.core.skills.discovery import SkillCandidate; "
    "from jarvis.core.skills.loader import import_module, find_skill_class; "
    "c = SkillCandidate(name=sys.argv[1], path=Path(sys.argv[2]), parent=sys.argv[3]); "
    "find_skill_class(import_module(c), path=c.path)"
)


# --- что изменилось на диске ------------------------------------------------


def fingerprint(paths: Iterable[Path]) -> dict[str, tuple[int, int]]:
    """Отпечаток файлов: путь → (время изменения, размер).

    Содержимое не читается: двести файлов опрашиваются за миллисекунды, а
    ложная тревога от `touch` обойдётся пробной сборкой, и только.
    """
    found: dict[str, tuple[int, int]] = {}
    for path in paths:
        if path.is_file():
            files: Iterable[Path] = (path,)
        elif path.is_dir():
            files = (
                item for item in path.rglob("*")
                if item.suffix in WATCHED and "__pycache__" not in item.parts
            )
        else:
            continue
        for item in files:
            try:
                stat = item.stat()
            except OSError:
                continue
            found[str(item)] = (stat.st_mtime_ns, stat.st_size)
    return found


def changed(before: Mapping[str, tuple[int, int]], after: Mapping[str, tuple[int, int]]) -> list[str]:
    """Что поменялось: изменённые, новые и удалённые файлы, по именам."""
    names = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    return sorted(Path(name).name for name in names)


def skill_prints(candidates: Iterable[SkillCandidate]) -> dict[str, dict[str, tuple[int, int]]]:
    """Отпечатки скиллов: метка (`browser/page`) → отпечаток её файлов.

    Файл подскилла лежит внутри папки главного, но принадлежит подскиллу: каждый
    файл относится к самой глубокой папке скилла, в которой лежит. Иначе правка
    `page` переподключала бы весь браузер.
    """
    folders = {candidate.label: candidate.path.parent for candidate in candidates}
    prints: dict[str, dict[str, tuple[int, int]]] = {label: {} for label in folders}
    by_depth = sorted(folders.items(), key=lambda item: len(item[1].parts), reverse=True)
    for label, folder in folders.items():
        if not folder.is_dir():
            continue
        for item in folder.rglob("*"):
            if item.suffix not in SKILL_FILES or "__pycache__" in item.parts:
                continue
            owner = next((name for name, where in by_depth if where in item.parents), label)
            if owner != label:
                continue
            try:
                stat = item.stat()
            except OSError:
                continue
            prints[label][str(item)] = (stat.st_mtime_ns, stat.st_size)
    return prints


def skill_check(candidate: SkillCandidate, *, root: Path, timeout: float = BUILD_TIMEOUT_S) -> str:
    """Импортировать скилл с диска в отдельном процессе; пусто — импортируется."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        done = subprocess.run(  # noqa: S603 — наш интерпретатор и наш код
            [sys.executable, "-c", _IMPORT_SKILL, candidate.name, str(candidate.path), candidate.parent],
            cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=flags, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"пробный импорт не запустился: {exc}"
    if done.returncode == 0:
        return ""
    tail = (done.stderr or done.stdout or "").strip().splitlines()
    return tail[-1] if tail else f"код выхода {done.returncode}"


def updated_line(count: int) -> str:
    """«{address}, на два модуля пришли обновления, обновил.»"""
    modules = plural_form(count, ("модуль", "модуля", "модулей"))
    came = "пришло обновление" if count % 10 == 1 and count % 100 != 11 else "пришли обновления"
    return f"{{address}}, на {count} {modules} {came}, обновил."


def build_check(config: Path, *, root: Path, timeout: float = BUILD_TIMEOUT_S) -> str:
    """Собрать систему с диска в отдельном процессе.

    :return: пусто, если собралась; иначе хвост ошибки.
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        done = subprocess.run(  # noqa: S603 — наш интерпретатор и наш код
            [sys.executable, "-c", _BUILD, str(config)],
            cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=flags, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"пробная сборка не запустилась: {exc}"
    if done.returncode == 0:
        return ""
    tail = (done.stderr or done.stdout or "").strip().splitlines()
    return tail[-1] if tail else f"код выхода {done.returncode}"


# --- просьба перезапуститься -------------------------------------------------


class Lifecycle:
    """Выключение и перезапуск: кто попросил и что делать после остановки.

    Сам процесс перезапускает не он, а тот, кто его запустил, — трей
    (`tray/session.py`): новый процесс поднимается, когда старый отпустит место.
    Поэтому без трея перезапуска нет (`restartable`), и инструмент честно
    об этом говорит, а не выключает ассистента с обещанием вернуться.
    """

    def __init__(
        self,
        stopping: asyncio.Event,
        *,
        check: Callable[[], Awaitable[str]] | None = None,
        code_changed: Callable[[], bool] | None = None,
    ) -> None:
        self.stopping = stopping
        #: Кто перезапустит процесс; ставит запуск из трея.
        self.restartable = False
        #: Попросили перезапуск, а не выключение.
        self.restarting = False
        #: Перезапуск сам, без прощания и приветствия: владелец его не просил.
        self.quietly = False
        self.reason = ""
        self._check = check
        #: Отличается ли код на диске от запущенного: не отличается — собирать незачем.
        self.code_changed: Callable[[], bool] = code_changed or (lambda: True)

    def shutdown(self) -> None:
        """Выключиться насовсем."""
        self.stopping.set()

    async def restart(self, reason: str, *, quietly: bool = False, checked: bool = False) -> str:
        """Перезапуститься.

        :param checked: новый код уже собран пробно — второй раз незачем.
        :return: пусто, если перезапуск начат; иначе — почему нет.
        """
        if not self.restartable:
            return "перезапуск умеет только Jarvis из трея"
        if not checked and self._check is not None and self.code_changed():
            error = await self._check()
            if error:
                logger.warning("Перезапуск отменён: новый код не собирается — %s", error)
                return f"новый код не собирается: {error}"
        logger.info("Перезапускаюсь%s: %s", " сам" if quietly else "", reason)
        self.restarting, self.quietly, self.reason = True, quietly, reason
        self.stopping.set()
        return ""

    def requested_from_tray(self) -> None:
        """Перезапуск из меню трея: проверять нечего, человек сам нажал."""
        self.restarting, self.reason = True, "меню трея"
        self.stopping.set()


# --- что переживает перезапуск ------------------------------------------------


@dataclass(frozen=True, slots=True)
class Carried:
    """Что вернулось из прошлого запуска."""

    restart: bool
    quietly: bool
    unfinished: tuple[str, ...]
    language: str = "ru"


def _intent(data: Mapping[str, Any]) -> Intent:
    return Intent(tool=str(data["tool"]), arguments=dict(data.get("arguments") or {}))


class Carryover:
    """Состояние между запусками: сохранить на выходе, вернуть на входе.

    Возвращается только свежее (`max_age_s`): режим «не слушаю», включённый
    вчера вечером, сегодня утром уже никто не ждёт. Файл читается один раз и
    удаляется — упавший посреди старта процесс не вернёт то же самое дважды.
    """

    def __init__(
        self,
        path: Path,
        *,
        modes: Modes,
        conversation: Conversation,
        dispatcher: Dispatcher,
        jobs: Jobs,
        max_age_s: float = 180.0,
    ) -> None:
        self._path = path
        self._modes = modes
        self._conversation = conversation
        self._dispatcher = dispatcher
        self._jobs = jobs
        self._max_age = max_age_s

    def snapshot(self, *, restart: bool, quietly: bool) -> dict[str, Any]:
        """Что записать: всё в относительных секундах — монотонные часы у процесса свои."""
        now = time.monotonic()
        question = self._dispatcher.awaiting
        pending: dict[str, Any] | None = None
        if question is not None and question.alive():
            pending = {
                "tool": question.intent.tool,
                "arguments": dict(question.intent.arguments),
                "question": question.question,
                "language": question.language,
                "choices": [{"tool": c.tool, "arguments": dict(c.arguments)} for c in question.choices],
                "until": question.until,
            }
        running = self._jobs.running
        return {
            "saved_at": time.time(),
            "restart": restart,
            "quietly": quietly,
            "modes": [
                {"name": mode.name, "left_s": mode.remaining(now)}
                for mode in self._modes.all() if mode.name in CARRIED_MODES
            ],
            "turns": [
                {"role": turn.role, "text": turn.text, "age_s": now - turn.at}
                for turn in self._conversation.turns(now=now)
            ],
            "pending": pending,
            "unfinished": [job.title for job in running],
            "language": running[0].language if running else "ru",
        }

    def save(self, *, restart: bool, quietly: bool = False) -> None:
        """Записать состояние. Молча: выключению мешать нельзя."""
        try:
            data = self.snapshot(restart=restart, quietly=quietly)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data, ensure_ascii=False, default=str), "utf-8")
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Состояние на перезапуск не записалось: %s", exc)

    def restore(self) -> Carried | None:
        """Вернуть сохранённое, если оно свежее."""
        try:
            data = json.loads(self._path.read_text("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning("Состояние прошлого запуска не прочиталось: %s", exc)
            data = None
        try:
            self._path.unlink()
        except OSError:
            pass
        if not isinstance(data, dict):
            return None
        age = time.time() - float(data.get("saved_at") or 0)
        if not 0 <= age <= self._max_age:
            logger.info("Состояние прошлого запуска старое (%.0f с) — не возвращаю", age)
            return None
        self._apply(data, age)
        return Carried(
            restart=bool(data.get("restart")),
            quietly=bool(data.get("quietly")),
            unfinished=tuple(str(title) for title in data.get("unfinished") or ()),
            language=str(data.get("language") or "ru"),
        )

    def _apply(self, data: Mapping[str, Any], age: float) -> None:
        now = time.monotonic()
        modes = 0
        for mode in data.get("modes") or ():
            left = float(mode.get("left_s") or 0)
            name = str(mode.get("name") or "")
            if name not in CARRIED_MODES:
                continue
            if left == 0:
                self._modes.on(name)
            elif left > age:
                self._modes.on(name, minutes=(left - age) / 60)
            else:
                continue
            modes += 1
        turns = list(data.get("turns") or ())
        for turn in turns:
            at = now - age - float(turn.get("age_s") or 0)
            text = str(turn.get("text") or "")
            if turn.get("role") == "user":
                self._conversation.said(text, now=at)
            else:
                self._conversation.replied(text, now=at)
        asked = data.get("pending")
        question = ""
        if isinstance(asked, dict) and time.time() < float(asked.get("until") or 0):
            try:
                self._dispatcher.ask_again(
                    Pending(
                        intent=_intent(asked),
                        question=str(asked.get("question") or ""),
                        language=str(asked.get("language") or "ru"),
                        choices=tuple(_intent(choice) for choice in asked.get("choices") or ()),
                        until=float(asked["until"]),
                    )
                )
                question = f", вопрос про {asked['tool']}"
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Заданный вопрос не вернулся: %s", exc)
        logger.info(
            "Вернул состояние прошлого запуска (%.0f с назад): режимов %d, реплик %d%s",
            age, modes, len(turns), question,
        )


# --- перезапуск сам ------------------------------------------------------------


class CoreWatch:
    """Замечает, что ядро на диске новее запущенного, и перезапускает, когда можно.

    «Когда можно» — это всё сразу: файлы перестали меняться (`settle_s`, иначе
    перезапуск попал бы посреди правки), владелец давно ничего не говорил
    (`idle_s`), нет поручений, заданного вопроса и тихого режима, ничего не
    играет, и новый код собирается. Звук проверяет инструмент из конфига
    (`busy_tool`): ядро про звуковые сессии Windows не знает и знать не должно.
    """

    #: События, после которых владелец считается занятым разговором.
    #:
    #: Вызова инструмента тут нет намеренно: его делают и сами скиллы по своим
    #: часам, и этот же наблюдатель, спрашивая «что играет». С ним отсчёт тишины
    #: сбрасывал себя сам, и перезапуск ждал вечно (лог 29.09.2026: «недавно
    #: разговаривали» через раз, хотя разговора не было). Команда владельца и
    #: так видна — по распознанной речи, набранному тексту и ответу.
    ACTIVE = (
        "voice.wake_word.detected", "voice.command.recognized", "input.command.typed",
        "assistant.speaking", "assistant.announcement",
    )

    def __init__(
        self,
        *,
        lifecycle: Lifecycle,
        settings: RestartConfig,
        watched: Iterable[Path],
        events: EventBus | None = None,
        modes: Modes | None = None,
        jobs: Jobs | None = None,
        dispatcher: Dispatcher | None = None,
        registry: ToolRegistry | None = None,
        announcer: Announcer | None = None,
        check: Callable[[], Awaitable[str]] | None = None,
        skills: SkillManager | None = None,
        root: Path | None = None,
    ) -> None:
        self._lifecycle = lifecycle
        self._settings = settings
        self._watched = tuple(watched)
        self._events = events
        self._modes = modes
        self._jobs = jobs
        self._dispatcher = dispatcher
        self._registry = registry
        self._announcer = announcer
        self._check = check
        self._baseline: dict[str, tuple[int, int]] = {}
        self._seen: dict[str, tuple[int, int]] = {}
        self._seen_at = 0.0
        self._active_at = time.monotonic()
        #: Отпечаток, о котором уже сказано или который не собрался: не повторять.
        self._settled: dict[str, tuple[int, int]] | None = None
        self._last_busy = ""
        self._task: asyncio.Task[None] | None = None
        #: Скиллы: отпечатки на момент загрузки каждого и что видели в прошлый раз.
        self._skills = skills
        self._root = root or Path.cwd()
        self._skill_base: dict[str, dict[str, tuple[int, int]]] = {}
        self._skill_seen: dict[str, dict[str, tuple[int, int]]] = {}
        self._skill_seen_at = 0.0

    @property
    def service_name(self) -> str:
        return "core-watch"

    def code_changed(self) -> bool:
        """Отличается ли ядро на диске от запущенного."""
        return fingerprint(self._watched) != self._baseline

    async def start(self) -> None:
        self._baseline = await asyncio.to_thread(fingerprint, self._watched)
        if self._skills is not None:
            self._skill_base = await asyncio.to_thread(skill_prints, self._skills.candidates())
        if self._events is not None:
            for name in self.ACTIVE:
                self._events.subscribe(name, self._on_activity)
            self._events.subscribe(SkillLoaded.NAME, self._on_skill_loaded)
        if self._settings.auto != "off" or self._settings.skills != "off":
            self._task = asyncio.create_task(self._loop(), name="core-watch")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _on_activity(self, event: object) -> None:
        self._active_at = time.monotonic()

    async def _on_skill_loaded(self, event: object) -> None:
        """Скилл загрузили — кто угодно: голосом, из панели, мы сами.

        Его новый отпечаток и есть новая точка отсчёта. Иначе после «переподключи
        модуль» он переподключился бы второй раз, а доклад соврал бы про обновление.
        """
        if self._skills is None or not isinstance(event, SkillLoaded):
            return
        candidate = self._skills.candidate(event.skill)
        if candidate is not None:
            # Считать по всем скиллам, а не по одному: иначе файлы подскилла
            # (`browser/page`) засчитываются главному, отпечаток не сходится с
            # полным обходом, и ручное «переподключи модуль браузер» через
            # полминуты повторялось само — с докладом (живой лог 29.09.2026).
            prints = skill_prints(self._skills.candidates())
            self._skill_base[candidate.label] = prints.get(candidate.label, {})

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._settings.check_s)
            try:
                core = await self.tick() if self._settings.auto != "off" else "same"
                # Ядро ждёт перезапуска — скиллы подхватятся вместе с ним.
                if core == "same":
                    await self.skills_tick()
            except Exception:  # noqa: BLE001 — наблюдатель не должен ронять ассистента
                logger.exception("Наблюдатель за ядром споткнулся")

    async def skills_tick(self, now: float | None = None) -> str:
        """Переподключить скиллы, обновившиеся на диске. Возвращает, что решил."""
        if self._skills is None or self._settings.skills == "off":
            return "off"
        moment = time.monotonic() if now is None else now
        candidates = {candidate.label: candidate for candidate in self._skills.candidates()}
        current = await asyncio.to_thread(skill_prints, candidates.values())
        changed = sorted(
            label for label in current if current[label] != self._skill_base.get(label)
        )
        if not changed:
            self._skill_seen = {}
            return "same"
        if current != self._skill_seen:
            if not self._skill_seen:
                logger.info("Скиллы на диске обновились: %s", ", ".join(changed))
            self._skill_seen, self._skill_seen_at = current, moment
            return "changing"
        if moment - self._skill_seen_at < SKILL_SETTLE_S:
            return "settling"
        if moment - self._active_at < SKILL_IDLE_S or (self._jobs is not None and self._jobs.running):
            return "busy"

        updated: list[str] = []
        failed: list[str] = []
        disabled = self._skills.disabled
        for label in changed:
            candidate = candidates[label]
            # Главный переподключается вместе с подскиллами — отдельно их не трогаем.
            if candidate.parent in changed or candidate.name in disabled:
                continue
            error = await asyncio.to_thread(skill_check, candidate, root=self._root)
            if error:
                logger.warning("Скилл %s обновился, но не импортируется — оставляю прежний: %s", label, error)
                failed.append(label)
                continue
            try:
                loaded = self._skills.resolve(candidate.name)
                if loaded in self._skills.loaded:
                    await self._skills.reload(loaded)
                else:
                    await self._skills.adopt(candidate.name)
            except Exception as exc:  # noqa: BLE001 — один скилл не роняет наблюдателя
                logger.warning("Скилл %s после обновления не поднялся: %s", label, exc)
                failed.append(label)
                continue
            updated.append(label)
        # Точка отсчёта — то, что на диске сейчас: не вышедшее ждёт новой правки,
        # а не пробуется каждые полминуты.
        self._skill_base.update({label: current[label] for label in changed})
        self._skill_seen = {}
        if updated:
            logger.info("Скиллы переподключены после обновления: %s", ", ".join(updated))
        self._tell_skills(updated, failed)
        return "updated" if updated else "failed"

    def _tell_skills(self, updated: list[str], failed: list[str]) -> None:
        if self._settings.skills != "tell" or self._announcer is None:
            return
        if updated:
            self._announcer.offer(updated_line(len(updated)), importance=LOW)
        for label in failed:
            self._announcer.offer(
                f"{{address}}, модуль {label} после обновления не поднялся, подробности в логе.",
                importance=NORMAL,
            )

    async def tick(self, now: float | None = None) -> str:
        """Один взгляд на диск. Возвращает, что решил, — для тестов и лога."""
        moment = time.monotonic() if now is None else now
        current = await asyncio.to_thread(fingerprint, self._watched)
        if current == self._baseline:
            self._seen, self._settled = {}, None
            return "same"
        if current != self._seen:
            if not self._seen:
                logger.info("Ядро на диске обновилось: %s", ", ".join(changed(self._baseline, current)[:8]))
            self._seen, self._seen_at = current, moment
            return "changing"
        if moment - self._seen_at < self._settings.settle_s:
            return "settling"
        if self._settled == current:
            return "settled"
        busy = await self.busy(moment)
        if busy:
            if busy != self._last_busy:
                logger.info("Перезапуск на новое ядро подождёт: %s", busy)
                self._last_busy = busy
            return "busy"
        self._last_busy = ""
        if self._check is not None:
            error = await self._check()
            if error:
                logger.warning("Новое ядро не собирается, перезапуск откладываю: %s", error)
                self._settled = current
                return "broken"
        if self._settings.auto == "tell":
            self._settled = current
            if self._announcer is not None:
                self._announcer.offer(_TELL)
            return "told"
        files = ", ".join(changed(self._baseline, current)[:5])
        error = await self._lifecycle.restart(f"обновилось ядро ({files})", quietly=True, checked=True)
        if error:
            self._settled = current
            return "refused"
        return "restart"

    async def busy(self, now: float | None = None) -> str:
        """Чем владелец сейчас занят; пусто — ничем, можно перезапускаться."""
        moment = time.monotonic() if now is None else now
        if moment - self._active_at < self._settings.idle_s:
            return "недавно разговаривали"
        if self._jobs is not None and self._jobs.running:
            return "идёт поручение"
        if self._dispatcher is not None and self._dispatcher.awaiting is not None:
            return "жду ответа на вопрос"
        if self._modes is not None and self._modes.active(QUIET):
            return "тихий режим"
        tool = self._settings.busy_tool
        if tool and self._registry is not None and self._registry.has(tool):
            result = await self._registry.invoke(tool, {})
            playing = result.value.get("playing") if result.ok and isinstance(result.value, dict) else None
            if playing:
                return f"играет звук: {', '.join(playing)}"
        return ""


def announce_unfinished(events: EventBus, carried: Carried) -> None:
    """Сказать, что брошено перезапуском: на поручение надеялись."""
    if not carried.unfinished:
        return
    template = _UNFINISHED.get(carried.language, _UNFINISHED["ru"])
    events.emit(
        AnnouncementRequested(
            source="restart", text=template.format(titles="; ".join(carried.unfinished)),
            language=carried.language,
        )
    )


def restart_file(memory_dir: str | os.PathLike[str]) -> Path:
    """Где лежит состояние между запусками."""
    return Path(memory_dir) / "restart.json"
