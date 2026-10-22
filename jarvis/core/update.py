"""Обновление из git: новое в `main` приезжает на машину само.

Просьба владельца 20.10.2026: «чтобы он реагировал не только на изменение кода,
но и на пуши в гите, чтобы не закидывать код каждый раз по ssh». Перезапуск на
новое ядро и переподключение скиллов уже умеет `CoreWatch` (`core/restart.py`),
но он смотрит на диск — а на диск код до сих пор клали руками.

Поэтому здесь ровно одно звено: раз в `check_s` секунд `git fetch`, и если
ветка отстала от своей удалённой — `git merge --ff-only`. Дальше всё как при
правке руками: файлы на диске новые, `CoreWatch` ждёт тишины, собирает новое
ядро пробно и перезапускается, а скиллы переподключает без перезапуска. Второго
механизма перезапуска не появилось, и правила «когда можно» остались одни.

**Только перемотка вперёд.** Свои коммиты на машине (ветки разошлись) и правка,
которую слияние перезаписало бы, — повод промолчать и записать в лог, а не
сливать: машина владельца не место для разрешения конфликтов. Незакоммиченное,
которого новые коммиты не касаются, слиянию не мешает — так его делает сам git.
Не та ветка — тоже не трогаем: владелец её проверяет.

**Зависимости сами не ставятся.** Поменялся `pyproject.toml` — об этом говорится
вслух: `pip install` на живой машине без спроса может сломать больше, чем
починит.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.core.attention import NORMAL
from jarvis.core.contracts import ToolResult
from jarvis.core.tools import collect_tools, tool
from jarvis.core.tts.normalize import plural_form

if TYPE_CHECKING:
    from jarvis.core.attention import Announcer
    from jarvis.core.config.schema import UpdateConfig
    from jarvis.core.tools import ToolRegistry

logger = logging.getLogger(__name__)

#: Сколько ждать одну команду git. Сеть ноутбука бывает медленной, но висящий
#: `fetch` (спросил пароль, завис на рукопожатии) не должен жить вечно.
GIT_TIMEOUT_S = 120.0

#: Итоги одной попытки.
SAME = "same"            # нового нет
UPDATED = "updated"      # перемотали вперёд
NO_GIT = "no-git"        # не клон или git не найден
OTHER_BRANCH = "other-branch"
DIVERGED = "diverged"    # на машине свои коммиты
FAILED = "failed"        # сеть, доступ, слияние отказало

#: Файлы, правка которых требует перезапуска, а не переподключения скилла.
_CORE = ("jarvis/", "config/config.yaml")
_DEPENDENCIES = "pyproject.toml"

_DEPS_LINE = (
    "{address}, в обновлении поменялись зависимости. "
    "Нужно поставить пакеты заново, сам этого делать не стал."
)


@dataclass(frozen=True, slots=True)
class Pulled:
    """Что вышло из одной попытки обновиться."""

    status: str
    before: str = ""
    after: str = ""
    #: Сколько коммитов приехало и какие файлы они тронули.
    commits: int = 0
    files: tuple[str, ...] = ()
    #: Почему не вышло — для лога; вслух не читается.
    reason: str = ""

    @property
    def core(self) -> bool:
        """Тронуто ли ядро — тогда нужен перезапуск, а не переподключение скилла."""
        return any(name.startswith(_CORE) for name in self.files)

    @property
    def dependencies(self) -> bool:
        return _DEPENDENCIES in self.files


class GitError(Exception):
    """Команда git не выполнилась; текст — её последняя строка."""


#: Переменные прокси, которые git берёт из окружения.
_PROXY_VARS = frozenset({"https_proxy", "http_proxy", "all_proxy"})


def _git_env(*, proxy: bool) -> dict[str, str]:
    """Окружение для git: без вопросов в пустоту, с прокси или без него."""
    env = {key: value for key, value in os.environ.items()
           if proxy or key.lower() not in _PROXY_VARS}
    # Никаких вопросов в пустоту: окна нет, спросить пароль некого.
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _proxied() -> bool:
    return any(key.lower() in _PROXY_VARS for key in os.environ)


def _fetch(root: Path, git: str) -> None:
    """Скачать новое: через `HTTPS_PROXY` из `.env`, а не вышло — напрямую.

    Одного пути мало, и оба уже отказывали. Прокси (туннель на сервер) лёг
    20.10.2026 вместе с адресом в Tailscale — обновление легло с ним. Напрямую
    GitHub с ноута сбрасывает соединение (замер 21.10.2026 из процесса Jarvis),
    хотя у владельца в его сеансе git ходит и так. Порядок — как у остального
    сетевого: прокси первым.
    """
    try:
        _git(root, git, "fetch", "--quiet")
    except GitError as first:
        if not _proxied():
            raise
        try:
            _git(root, git, "fetch", "--quiet", proxy=False)
        except GitError as second:
            raise GitError(f"{first}; напрямую — {second}") from second


def _git(root: Path, git: str, *args: str, proxy: bool = True, timeout: float = GIT_TIMEOUT_S) -> str:
    """Выполнить git в клоне и вернуть вывод; отказ — `GitError`.

    `safe.directory` указан явно: Jarvis у владельца запущен с правами
    администратора, а клон принадлежит обычной учётной записи, и git отказался
    бы работать с «чужим» каталогом. Через `-c` это разрешено — в отличие от
    настройки внутри самого репозитория.
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    env = _git_env(proxy=proxy)
    try:
        done = subprocess.run(  # noqa: S603 — git из конфига, аргументы наши
            [git, "-c", f"safe.directory={root.as_posix()}", *args],
            cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=timeout, creationflags=flags, env=env, check=False,
        )
    except FileNotFoundError as exc:
        raise GitError(f"git не найден: {git}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {args[0]}: {exc}") from exc
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()
        raise GitError(f"git {args[0]}: {tail[-1] if tail else f'код выхода {done.returncode}'}")
    return done.stdout.strip()


def pull(root: Path, *, git: str = "git", branch: str = "main") -> Pulled:
    """Догнать удалённую ветку перемоткой вперёд. Блокирует — звать из потока."""
    if not (root / ".git").exists():
        return Pulled(NO_GIT, reason="не клон git")
    try:
        current = _git(root, git, "rev-parse", "--abbrev-ref", "HEAD")
        if branch and current != branch:
            return Pulled(OTHER_BRANCH, reason=f"сейчас ветка {current}, обновляется {branch}")
        upstream = "@{upstream}"
        _fetch(root, git)
        before = _git(root, git, "rev-parse", "HEAD")
        after = _git(root, git, "rev-parse", upstream)
        if before == after:
            return Pulled(SAME, before=before, after=after)
        try:
            _git(root, git, "merge-base", "--is-ancestor", "HEAD", upstream)
        except GitError:
            ahead = _git(root, git, "rev-list", "--count", f"{upstream}..HEAD")
            return Pulled(DIVERGED, before=before, after=after,
                          reason=f"на машине своих коммитов: {ahead}")
        commits = int(_git(root, git, "rev-list", "--count", f"HEAD..{upstream}") or 0)
        files = tuple(_git(root, git, "diff", "--name-only", "HEAD", upstream).splitlines())
        # Правку, которую новые коммиты перезаписали бы, git сам не тронет и откажет.
        _git(root, git, "merge", "--ff-only", "--quiet", upstream)
    except GitError as exc:
        status = NO_GIT if str(exc).startswith("git не найден") else FAILED
        return Pulled(status, reason=str(exc))
    return Pulled(UPDATED, before=before, after=after, commits=commits, files=files)


def updated_speech(pulled: Pulled) -> str:
    """«Скачал 3 коммита.» — сколько приехало, словами."""
    word = plural_form(pulled.commits, ("коммит", "коммита", "коммитов"))
    return f"Скачал {pulled.commits} {word}."


def modules_speech(updated: list[str], failed: list[str]) -> str:
    """«Обновил модули: page, relay.» — что переподключено и что нет, словами.

    Метка подскилла — путь («browser/page»), вслух идёт только его имя.
    """
    updated = [label.rsplit("/", 1)[-1] for label in updated]
    failed = [label.rsplit("/", 1)[-1] for label in failed]
    parts = []
    if updated:
        parts.append(f"Обновил {'модуль' if len(updated) == 1 else 'модули'}: {', '.join(updated)}.")
    if failed:
        word = "модуль" if len(failed) == 1 else "модули"
        verb = "не поднялся" if len(failed) == 1 else "не поднялись"
        parts.append(f"{word.capitalize()} {', '.join(failed)} {verb}, подробности в логе.")
    return " ".join(parts) or "Модули не менялись."


class GitUpdate:
    """Раз в `check_s` секунд догоняет ветку из git; голосом — «проверь обновления».

    Сам ничего не перезапускает по таймеру: новые файлы видит `CoreWatch` и
    решает, когда можно. Голосом другое дело — владелец сам попросил, и если
    приехало ядро, перезапуск идёт сразу (`restart`).
    """

    def __init__(
        self,
        *,
        settings: UpdateConfig,
        root: Path,
        registry: ToolRegistry | None = None,
        announcer: Announcer | None = None,
        restart: Callable[[str], Awaitable[str]] | None = None,
        reload_skills: Callable[[], Awaitable[tuple[list[str], list[str]]]] | None = None,
        pull: Callable[[], Pulled] | None = None,
    ) -> None:
        self._settings = settings
        self._root = root
        self._registry = registry
        self._announcer = announcer
        self._restart = restart
        #: Переподключить приехавшие скиллы сейчас же (`CoreWatch.reload_skills_now`).
        self._reload_skills = reload_skills
        self._pull = pull or (lambda: _default_pull(root, settings))
        self._task: asyncio.Task[None] | None = None
        self._registrations: list[Any] = []
        self._lock = asyncio.Lock()
        #: О чём уже сказано в лог: отказ на том же удалённом коммите не повторять
        #: каждые пять минут.
        self._reported = ""

    @property
    def service_name(self) -> str:
        return "git-update"

    async def start(self) -> None:
        register = getattr(self._registry, "register", None)
        if callable(register):
            for item in collect_tools(self, namespace="core"):
                self._registrations.append(register(item))
        if self._settings.enabled and (self._root / ".git").exists():
            self._task = asyncio.create_task(self._loop(), name="git-update")
            logger.info("Обновление из git: ветка %s, раз в %d с", self._settings.branch or "текущая",
                        int(self._settings.check_s))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        for registration in self._registrations:
            registration.revoke()
        self._registrations.clear()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._settings.check_s)
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — обновление не должно ронять ассистента
                logger.exception("Обновление из git споткнулось")

    async def tick(self) -> Pulled:
        """Одна попытка; итог пишется в лог, о зависимостях говорится вслух."""
        async with self._lock:
            pulled = await asyncio.to_thread(self._pull)
        self._log(pulled)
        if pulled.status == UPDATED and pulled.dependencies and self._announcer is not None:
            self._announcer.offer(_DEPS_LINE, importance=NORMAL)
        return pulled

    def _log(self, pulled: Pulled) -> None:
        if pulled.status == UPDATED:
            self._reported = ""
            logger.info(
                "Обновился из git: %s → %s, %d коммит(ов), файлов %d: %s",
                pulled.before[:7], pulled.after[:7], pulled.commits, len(pulled.files),
                ", ".join(pulled.files[:8]),
            )
            return
        if pulled.status == SAME:
            return
        key = f"{pulled.status}:{pulled.after}:{pulled.reason}"
        if key != self._reported:
            self._reported = key
            logger.warning("Обновление из git пропущено: %s", pulled.reason or pulled.status)

    # --- голосом ----------------------------------------------------------------

    @tool(
        name="update",
        phrases=[
            "проверь обновления",
            "обновись",
            "обнови себя",
            "скачай обновления",
            "есть обновления",
            "есть ли обновления",
            "update yourself",
            "check for updates",
        ],
        routable=False,
        reversible=False,
    )
    async def update(self) -> ToolResult:
        """Скачать новое из git сейчас же; приехало ядро — перезапуститься."""
        pulled = await self.tick()
        if pulled.status == SAME:
            return ToolResult.success({"updated": False}, speech={
                "ru": "Обновлений нет, всё свежее.", "en": "No updates, I'm up to date."})
        if pulled.status != UPDATED:
            return ToolResult.failure(pulled.reason or pulled.status, speech={
                "ru": _REFUSED.get(pulled.status, "Обновиться не вышло, подробности в логе."),
                "en": "I couldn't update, see the log."})
        speech = updated_speech(pulled)
        value: dict[str, object] = {"updated": True, "commits": pulled.commits, "core": pulled.core}
        if pulled.core and self._restart is not None:
            refused = await self._restart("обновление из git по команде владельца")
            if not refused:
                return ToolResult.success(value, speech={
                    "ru": f"{speech} Перезапускаюсь.", "en": "Updated. Restarting."})
            logger.warning("После обновления перезапуск отказал: %s", refused)
            if refused.startswith("новый код"):
                return ToolResult.success(value, speech={
                    "ru": f"{speech} Но новое ядро не собирается, остаюсь на старом. Подробности в логе.",
                    "en": "Updated, but the new core doesn't build."})
            return ToolResult.success(value, speech={
                "ru": f"{speech} Ядро новое — скажите «перезапустись», когда будет удобно.",
                "en": "Updated. Say restart when convenient."})
        if self._reload_skills is None:
            return ToolResult.success(value, speech={
                "ru": f"{speech} Модули подхватятся сами.", "en": "Updated. Modules will reload shortly."})
        # Просил владелец — не ждём тишины, как ждёт наблюдатель: переподключаем сразу.
        updated, failed = await self._reload_skills()
        value |= {"reloaded": updated, "failed": failed}
        return ToolResult.success(value, speech={
            "ru": f"{speech} {modules_speech(updated, failed)}",
            "en": f"Updated. Reloaded {len(updated)}, failed {len(failed)}." if updated or failed
            else "Updated. Nothing to reload."})


_REFUSED = {
    NO_GIT: "Я запущен не из клона git, обновлять нечего.",
    OTHER_BRANCH: "Сейчас открыта другая ветка, обновлять её не стал.",
    DIVERGED: "На этой машине есть свои коммиты, сливать сам не стал.",
}


def _default_pull(root: Path, settings: UpdateConfig) -> Pulled:
    return pull(root, git=settings.git, branch=settings.branch)

