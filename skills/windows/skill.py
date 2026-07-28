"""Управление компьютером студии: запуск и закрытие программ.

Скилл объявлен только для Windows. На других системах менеджер его пропустит —
это штатное поведение, а не ошибка сборки.
"""

from __future__ import annotations

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool


class WindowsSkill(Skill):
    """Запуск, закрытие и переключение программ на Windows."""

    meta = SkillMeta(
        name="windows",
        description="Управление программами на компьютере студии",
        version="0.1.0",
        platforms=("windows",),
    )

    async def on_setup(self) -> None:
        """Прочитать список известных программ из конфига."""
        self._programs: dict[str, str] = dict(
            self.context.setting(
                "programs",
                {
                    "steam": "steam.exe",
                    "obs": "obs64.exe",
                    "браузер": "chrome.exe",
                    "проводник": "explorer.exe",
                },
            )
        )
        self.log.info("Известных программ: %d", len(self._programs))

    @tool(phrases=["открой {program}", "запусти {program}",
                   "open {program}", "launch {program}"])
    async def launch_program(self, program: str) -> ToolResult:
        """Запустить программу по имени.

        :param program: короткое имя (steam, obs) или имя исполняемого файла.
        """
        executable = self._programs.get(program.strip().lower(), program)
        # TODO: subprocess.Popen / os.startfile
        self.log.info("Запуск программы: %s", executable)
        return ToolResult.success(
            {"program": program, "executable": executable},
            speech={"ru": f"Запускаю {program}.", "en": f"Launching {program}."},
        )

    @tool(phrases=["закрой {program}", "заверши {program}",
                   "close {program}", "quit {program}"])
    async def close_program(self, program: str) -> ToolResult:
        """Закрыть программу по имени.

        :param program: короткое имя или имя процесса.
        """
        executable = self._programs.get(program.strip().lower(), program)
        # TODO: taskkill / psutil
        self.log.info("Закрытие программы: %s", executable)
        return ToolResult.success(
            {"program": program, "executable": executable},
            speech={"ru": f"Закрываю {program}.", "en": f"Closing {program}."},
        )

    @tool(phrases=["какие программы открыты", "что запущено", "what is running"])
    async def list_programs(self) -> ToolResult:
        """Перечислить запущенные программы."""
        # TODO: psutil.process_iter
        running: list[str] = []
        return ToolResult.success(
            running,
            speech={"ru": "Список процессов пока не читается.",
                    "en": "Process listing isn't implemented yet."},
        )

    @tool()
    async def set_volume(self, level: int) -> ToolResult:
        """Установить громкость системы.

        :param level: громкость в процентах, от 0 до 100.
        """
        level = max(0, min(100, level))
        # TODO: pycaw
        return ToolResult.success(
            level,
            speech={"ru": f"Громкость {level} процентов.", "en": f"Volume {level} percent."},
        )

    async def health(self) -> HealthStatus:
        """Скилл-заглушка всегда считается работоспособным."""
        return HealthStatus.healthy("заглушка")
