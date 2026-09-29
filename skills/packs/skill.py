"""Наборы команд для программ: «фраза → сочетание клавиш в нужной программе».

Идея взята у Luxify Assistant (29.09.2026): у них около тысячи таких команд в
48 наборах — Discord, OBS, Photoshop, игры. Внутри это почти всегда горячая
клавиша самой программы, и значит, **это данные, а не код**: набор — YAML-файл
в `packs/`, новый набор пишется без Python, работает без модели и без сети.

Набор выглядит так::

    name: FL Studio
    processes: [FL64.exe]              # чьё окно — по имени процесса
    places: ["во фл", "в фл студии"]    # как программу называют в конце фразы
    commands:
      save:
        phrases: ["сохрани проект"]
        keys: ctrl+s                    # или список сочетаний по порядку
        say: "Сохранил."                # необязательно

**Две фразы у каждой команды, и это не дубль.** С названием программы
(«сохрани проект во фл») она зовёт именно её и честно отвечает, если программа
не запущена. Без названия («сохрани проект») — срабатывает, **только когда
программа открыта**, иначе фраза уходит дальше по цепочке: общие слова вроде
«сохрани» не должны отнимать команды у всей системы из-за набора, которым
сейчас не пользуются.

**Клавиши получает окно программы.** Не впереди — выводим его
(`windows.focus_window`), нажимаем и возвращаем то, что было впереди: команда
для FL Studio не должна выдёргивать владельца из браузера. Нажатие —
`windows.press_keys`: скилл не импортирует скилл.

В каталог модели наборы не идут: команд сотни, а платится каталог на каждой
неузнанной фразе. Их узнают фразы — даром.
"""

from __future__ import annotations

import asyncio
import functools
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import Tool, ToolSpec

PACKS = Path(__file__).with_name("packs")
FOCUS_TOOL = "windows.focus_window"
PRESS_TOOL = "windows.press_keys"
#: Сколько дать окну после вывода вперёд, прежде чем жать клавиши, секунд.
FOCUS_SETTLE_S = 0.15
#: Пауза между сочетаниями в одной команде, секунд.
STEP_PAUSE_S = 0.05
_EMPTY_SCHEMA: Mapping[str, Any] = {"type": "object", "properties": {}}


@dataclass(frozen=True, slots=True)
class Command:
    """Одна команда набора."""

    id: str
    phrases: tuple[str, ...]
    keys: tuple[str, ...]
    say: str = ""
    #: Выводить ли окно программы вперёд. Нет — для глобальных горячих клавиш
    #: (OBS, Discord с «глобальными» сочетаниями): им фокус не нужен.
    focus: bool = True
    reversible: bool = False
    #: Срабатывает ли без названия программы. Нет — для общих слов («пауза»,
    #: «перемотай»): у браузера и плееров они свои, и набор их не отнимает.
    alone: bool = True


@dataclass(frozen=True, slots=True)
class Pack:
    """Набор команд одной программы."""

    id: str
    name: str
    processes: frozenset[str]
    places: tuple[str, ...]
    commands: tuple[Command, ...]


class PackError(ValueError):
    """Набор описан с ошибкой — называем файл и место."""


def _phrases(value: Any) -> tuple[str, ...]:
    items = [value] if isinstance(value, str) else list(value or ())
    return tuple(" ".join(str(item).lower().replace("ё", "е").split()) for item in items if str(item).strip())


def parse_pack(pack_id: str, data: Mapping[str, Any]) -> Pack:
    """Набор из разобранного YAML. Ошибка описания — `PackError` с понятным текстом."""
    name = str(data.get("name") or pack_id)
    processes = frozenset(str(item).lower() for item in data.get("processes") or ())
    if not processes:
        raise PackError(f"набор {pack_id}: не указаны processes — чьё окно получает клавиши")
    commands: list[Command] = []
    for command_id, body in (data.get("commands") or {}).items():
        if not isinstance(body, Mapping):
            raise PackError(f"набор {pack_id}, команда {command_id}: ожидался словарь")
        phrases = _phrases(body.get("phrases"))
        keys = tuple(str(item) for item in ([body["keys"]] if isinstance(body.get("keys"), str) else body.get("keys") or ()))
        if not phrases or not keys:
            raise PackError(f"набор {pack_id}, команда {command_id}: нужны и phrases, и keys")
        commands.append(
            Command(
                id=str(command_id), phrases=phrases, keys=keys, say=str(body.get("say") or ""),
                focus=bool(body.get("focus", True)), reversible=bool(body.get("reversible", False)),
                alone=bool(body.get("alone", True)),
            )
        )
    return Pack(id=pack_id, name=name, processes=processes, places=_phrases(data.get("places")),
                commands=tuple(commands))


def load_packs(folders: Sequence[Path]) -> tuple[list[Pack], list[str]]:
    """Все наборы из папок. Сломанный файл не роняет остальные — он в списке ошибок."""
    packs: list[Pack] = []
    errors: list[str] = []
    for folder in folders:
        for path in sorted(folder.glob("*.yaml")):
            try:
                packs.append(parse_pack(path.stem, yaml.safe_load(path.read_text("utf-8")) or {}))
            except (OSError, yaml.YAMLError, PackError) as exc:
                errors.append(f"{path.name}: {exc}")
    return packs, errors


def placed(command: Command, pack: Pack) -> tuple[str, ...]:
    """Фразы с названием программы: «сохрани проект» + «во фл»."""
    return tuple(f"{phrase} {place}" for phrase in command.phrases for place in pack.places)


# --- окна Windows ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Window:
    title: str
    image: str
    front: bool


def top_windows() -> list[Window]:
    """Видимые окна с заголовком и именем процесса. Только Windows, несколько мс.

    Своё, а не `windows.list_windows`: проверка «открыта ли программа» идёт на
    каждой фразе, совпавшей с командой набора, и обязана быть синхронной.
    """
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    front = user32.GetForegroundWindow()
    found: list[Window] = []
    names: dict[int, str] = {}

    def image_of(pid: int) -> str:
        if pid not in names:
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            path = ""
            if handle:
                buffer = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(buffer))
                if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    path = buffer.value
                kernel32.CloseHandle(handle)
            names[pid] = Path(path).name.lower()
        return names[pid]

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd: int, _: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append(Window(title=title.value, image=image_of(pid.value), front=hwnd == front))
        return True

    user32.EnumWindows(visit, 0)
    return found


def pick_window(windows: Sequence[Window], pack: Pack) -> Window | None:
    """Окно программы: переднее, если оно её, иначе верхнее из её окон."""
    mine = [window for window in windows if window.image in pack.processes]
    return next((window for window in mine if window.front), mine[0] if mine else None)


def _is_open(pack: Pack, _arguments: Mapping[str, str]) -> bool:
    """Открыта ли программа набора — узнавать ли её фразу без названия."""
    return pick_window(top_windows(), pack) is not None


class PacksSkill(Skill):
    """Горячие клавиши программ голосом — наборами из YAML."""

    meta = SkillMeta(
        name="packs",
        description="Наборы команд для программ: фраза — горячая клавиша в нужной программе.",
        version="0.1.0",
        platforms=("windows",),
        requires=(FOCUS_TOOL, PRESS_TOOL),
        spoken=("наборы", "наборы команд", "packs"),
    )

    async def on_setup(self) -> None:
        extra = [Path(str(item)) for item in self.context.setting("folders", ()) or ()]
        disabled = {str(item) for item in self.context.setting("disabled", ()) or ()}
        packs, errors = load_packs([PACKS, *extra])
        self._packs = [pack for pack in packs if pack.id not in disabled]
        self._errors = errors
        for error in errors:
            self.log.warning("Набор команд не прочитался: %s", error)
        count = 0
        for pack in self._packs:
            for command in pack.commands:
                for tool in self._tools_for(pack, command):
                    self.context.scope.register_tool(tool)
                count += 1
        self.log.info(
            "Наборы команд: %s — команд %d",
            ", ".join(pack.name for pack in self._packs) or "нет", count,
        )

    async def health(self) -> HealthStatus:
        commands = sum(len(pack.commands) for pack in self._packs)
        detail = f"наборов {len(self._packs)}, команд {commands}"
        if self._errors:
            return HealthStatus.degraded(f"{detail}; с ошибкой: {'; '.join(self._errors)}")
        return HealthStatus.healthy(detail)

    def _tools_for(self, pack: Pack, command: Command) -> list[Tool]:
        """Два инструмента на команду: с названием программы и без него."""
        name = f"{self.meta.name}.{pack.id}_{command.id}"
        description = f"{pack.name}: {command.say.rstrip('.') or command.id} ({', '.join(command.keys)})"
        run = functools.partial(self._run, pack, command)
        tools = []
        named = placed(command, pack)
        if named:
            tools.append(Tool(spec=self._spec(name, description, named, command), handler=run))
        if not command.alone:
            return tools
        tools.append(
            Tool(
                spec=self._spec(f"{name}_here", description, command.phrases, command),
                handler=run,
                # Без названия программы — только когда она открыта: иначе общие
                # слова набора отнимали бы фразу у всей системы.
                recognizer=functools.partial(_is_open, pack),
            )
        )
        return tools

    def _spec(self, name: str, description: str, phrases: tuple[str, ...], command: Command) -> ToolSpec:
        return ToolSpec(
            name=name, description=description, parameters=_EMPTY_SCHEMA, phrases=phrases,
            skill=self.meta.name, routable=False, reversible=command.reversible,
        )

    async def _run(self, pack: Pack, command: Command) -> ToolResult:
        """Нажать команду в окне программы и вернуть то окно, что было впереди."""
        windows = await asyncio.to_thread(top_windows)
        target = pick_window(windows, pack)
        if target is None:
            return ToolResult.failure(
                f"{pack.name} не запущен",
                speech={"ru": f"{pack.name} не открыт.", "en": f"{pack.name} isn't open."},
            )
        was = next((window for window in windows if window.front), None)
        moved = False
        if command.focus and not target.front:
            focused = await self.tools.invoke(FOCUS_TOOL, {"title": target.title})
            if not focused.ok:
                return ToolResult.failure(
                    f"окно {pack.name} не вышло вперёд: {focused.error}",
                    speech={"ru": f"Не смог вывести {pack.name} вперёд.", "en": f"Couldn't bring {pack.name} up."},
                )
            moved = True
            await asyncio.sleep(FOCUS_SETTLE_S)
        for number, combination in enumerate(command.keys):
            if number:
                await asyncio.sleep(STEP_PAUSE_S)
            pressed = await self.tools.invoke(PRESS_TOOL, {"combination": combination})
            if not pressed.ok:
                return ToolResult.failure(
                    f"{pack.name}: не нажалось {combination}: {pressed.error}",
                    speech={"ru": "Не получилось нажать клавиши.", "en": "Couldn't press the keys."},
                )
        if moved and was is not None:
            await asyncio.sleep(FOCUS_SETTLE_S)
            await self.tools.invoke(FOCUS_TOOL, {"title": was.title})
        self.log.info("%s: %s (%s)", pack.name, command.id, " → ".join(command.keys))
        speech = {"ru": command.say} if command.say else None
        return ToolResult.success({"program": pack.name, "command": command.id, "keys": list(command.keys)},
                                  speech=speech)
