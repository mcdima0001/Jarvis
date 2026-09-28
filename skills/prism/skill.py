"""Prism Launcher: запустить сборку Minecraft сразу, а не открыть лаунчер.

Просьба владельца 28.09.2026: «он не запускает майн, просто открывает призм».
Сначала «майнкрафт» был псевдонимом Prism Launcher в настройках `windows` — это
открывало лаунчер, и сборку всё равно приходилось запускать руками. Prism умеет
запускать сборку сам: `prismlauncher.exe --launch <папка сборки>`, и если лаунчер
уже открыт, команда уходит в него.

**Какую сборку.** Без названия — ту, в которую играли последней (`lastLaunchTime`
в `instance.cfg`): у владельца их восемь, а играет он в одну. Названа («запусти
майн пвп дуо») — ищется по имени сборки, как её произнесли.

**Без прав администратора.** Jarvis работает с правами, и запущенное напрямую
их унаследовало бы. Программу с аргументами через `explorer.exe` не передать,
поэтому рядом кладётся ярлык с аргументами (`memory/prism/Minecraft.lnk`) и
открывается проводником — так же, как `windows` запускает всё остальное.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.text import best_match
from jarvis.core.tools import tool

#: Где Prism держит сборки и где лежит сам, если в настройках не сказано иное.
DATA_DIR = Path(os.environ.get("APPDATA", "")) / "PrismLauncher"
PROGRAM = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "PrismLauncher" / "prismlauncher.exe"
#: Насколько похожим должно быть названное имя сборки.
SIMILARITY = 0.6

_SHORTCUT_SCRIPT = r"""
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:JARVIS_LNK)
$s.TargetPath = $env:JARVIS_EXE
$s.Arguments = $env:JARVIS_ARGS
$s.WorkingDirectory = Split-Path $env:JARVIS_EXE
$s.Save()
"""


@dataclass(frozen=True, slots=True)
class Instance:
    """Сборка Prism: папка (её и ждёт `--launch`), имя и когда в неё играли."""

    folder: str
    name: str
    last_launch: int


def read_instances(data_dir: Path) -> list[Instance]:
    """Сборки из `instances/*/instance.cfg`. Чистая функция по файлам — её проверяют тесты."""
    found: list[Instance] = []
    root = data_dir / "instances"
    if not root.is_dir():
        return found
    for folder in sorted(root.iterdir()):
        config = folder / "instance.cfg"
        if not config.is_file():
            continue
        try:
            text = config.read_text("utf-8", errors="replace")
        except OSError:
            continue
        name = re.search(r"^name=(.*)$", text, re.MULTILINE)
        last = re.search(r"^lastLaunchTime=(\d+)", text, re.MULTILINE)
        found.append(
            Instance(
                folder=folder.name,
                name=(name.group(1).strip() if name else folder.name) or folder.name,
                last_launch=int(last.group(1)) if last else 0,
            )
        )
    return found


def pick(instances: list[Instance], asked: str) -> Instance | None:
    """Какую сборку запускать: названную или ту, в которую играли последней."""
    if not instances:
        return None
    asked = asked.strip()
    if not asked:
        return max(instances, key=lambda item: item.last_launch)
    names: dict[str, Instance] = {}
    for item in instances:
        names.setdefault(item.name, item)
        names.setdefault(item.folder, item)
    found = best_match(asked, list(names), similarity=SIMILARITY)
    return names[found] if found else None


def _is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.WinDLL("shell32").IsUserAnAdmin())
    except (OSError, AttributeError):
        return False


class PrismSkill(Skill):
    """Запускает сборку Minecraft через Prism Launcher."""

    meta = SkillMeta(
        name="prism",
        description="Prism Launcher: запуск сборки Minecraft",
        version="0.1.0",
        platforms=("windows",),
        spoken=("призм", "prism", "майнкрафт", "майн", "minecraft"),
    )

    async def on_setup(self) -> None:
        self._data = Path(str(self.context.setting("data_dir", "") or DATA_DIR))
        self._program = Path(str(self.context.setting("program", "") or PROGRAM))
        self._shortcut = Path(__file__).resolve().parents[2] / "memory" / "prism" / "Minecraft.lnk"

    @tool(
        phrases=[
            "запусти майн", "запусти майнкрафт", "запусти minecraft",
            # Прежняя длинная форма — тоже игра, а не лаунчер.
            "запусти майнкрафт через призм лаунчер", "запусти майнкрафт через prism launcher",
            "запусти minecraft через prism launcher",
            "открой майн", "открой майнкрафт", "открой minecraft", "включи майнкрафт",
            "запусти майн {instance}", "запусти майнкрафт {instance}", "запусти minecraft {instance}",
            "launch minecraft", "launch minecraft {instance}",
        ],
        reversible=True,
    )
    async def launch(self, instance: str = "") -> ToolResult:
        """Запустить Minecraft — сразу игру, не лаунчер.

        :param instance: какую сборку; пусто — ту, в которую играли последней.
        """
        instances = await asyncio.to_thread(read_instances, self._data)
        chosen = pick(instances, instance)
        if chosen is None:
            known = ", ".join(item.name for item in instances) or "ни одной"
            return ToolResult.failure(
                f"сборка {instance!r} не найдена среди: {known}",
                speech={
                    "ru": f"Не нашёл сборку {instance}. Есть: {known}." if instances
                    else "Не нашёл ни одной сборки Prism Launcher.",
                    "en": f"No instance called {instance}." if instances else "No Prism Launcher instances found.",
                },
            )
        if not self._program.is_file():
            return ToolResult.failure(
                f"нет {self._program}",
                speech={"ru": "Не нашёл Prism Launcher.", "en": "Prism Launcher isn't installed."},
            )
        try:
            await asyncio.to_thread(self._start, chosen)
        except (OSError, subprocess.SubprocessError) as exc:
            self.log.error("Minecraft не запустился: %s", exc)
            return ToolResult.failure(
                f"{type(exc).__name__}: {exc}",
                speech={"ru": "Не получилось запустить Minecraft.", "en": "Couldn't launch Minecraft."},
            )
        self.log.info("Запускаю Minecraft: сборка %r (%s)", chosen.name, chosen.folder)
        return ToolResult.success(
            {"instance": chosen.name, "folder": chosen.folder},
            speech={
                "ru": (f"Запускаю Minecraft, сборка {chosen.name}.", f"Minecraft, сборка {chosen.name}, запускается."),
                "en": (f"Launching Minecraft, {chosen.name}.",),
            },
        )

    def _start(self, chosen: Instance) -> None:
        """Запустить с обычными правами: ярлык с аргументами через проводник."""
        arguments = f'--launch "{chosen.folder}"'
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if not _is_admin() or sys.platform != "win32":
            subprocess.Popen([str(self._program), "--launch", chosen.folder], creationflags=flags)
            return
        self._shortcut.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, JARVIS_LNK=str(self._shortcut), JARVIS_EXE=str(self._program), JARVIS_ARGS=arguments)
        done = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _SHORTCUT_SCRIPT],
            capture_output=True, timeout=15, env=env, creationflags=flags, check=False,
        )
        if done.returncode != 0 or not self._shortcut.is_file():
            raise OSError(f"ярлык не создался: {done.stderr.decode('cp866', 'replace')[-200:]}")
        subprocess.Popen(["explorer.exe", str(self._shortcut)], creationflags=flags)

    async def health(self) -> HealthStatus:
        count = len(await asyncio.to_thread(read_instances, self._data))
        if not self._program.is_file():
            return HealthStatus.degraded(f"нет {self._program}")
        return HealthStatus.healthy(f"сборок: {count}")
