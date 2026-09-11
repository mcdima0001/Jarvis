"""Скилл сообщает, сколько времени компьютер работает с последней перезагрузки."""

from __future__ import annotations

import asyncio
import ctypes
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool

#: Платформы, для которых умеем читать время работы системы.
SUPPORTED_PLATFORMS = ("win32", "darwin", "linux")

#: Ошибки, которые может дать чтение времени работы системы.
UPTIME_ERRORS = (OSError, ValueError, RuntimeError, subprocess.SubprocessError)


def _plural(number: int, forms: tuple[str, str, str]) -> str:
    """Возвращает форму слова, подходящую числу: день, дня, дней."""
    if number % 100 in range(11, 15):
        return forms[2]
    if number % 10 == 1:
        return forms[0]
    if number % 10 in (2, 3, 4):
        return forms[1]
    return forms[2]


def _read_uptime_seconds() -> float:
    """Читает время работы системы в секундах, блокирующий вызов."""
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32")
        get_tick_count = kernel32.GetTickCount64
        get_tick_count.restype = ctypes.c_uint64
        return float(get_tick_count()) / 1000.0
    if sys.platform == "darwin":
        completed = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.boottime"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        match = re.search(r"sec\s*=\s*(\d+)", completed.stdout)
        if match is None:
            raise RuntimeError("не удалось разобрать ответ sysctl")
        return max(0.0, time.time() - float(match.group(1)))
    raw = Path("/proc/uptime").read_text(encoding="utf-8")
    return float(raw.split()[0])


def _describe_ru(seconds: float) -> str:
    """Собирает русскую фразу о времени работы: два дня три часа."""
    total = int(seconds)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days} {_plural(days, ('день', 'дня', 'дней'))}")
    if hours:
        parts.append(f"{hours} {_plural(hours, ('час', 'часа', 'часов'))}")
    if minutes and not days:
        parts.append(f"{minutes} {_plural(minutes, ('минуту', 'минуты', 'минут'))}")
    if not parts:
        parts.append("меньше минуты")
    return " ".join(parts)


def _describe_en(seconds: float) -> str:
    """Собирает английскую фразу о времени работы."""
    total = int(seconds)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes and not days:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if not parts:
        parts.append("less than a minute")
    return " ".join(parts)


class UptimeSkill(Skill):
    """Считает и произносит время работы компьютера с последней загрузки."""

    meta = SkillMeta(
        name="uptime",
        description="Сообщает, сколько времени компьютер работает без перезагрузки.",
        version="0.1.0",
        spoken=("аптайм", "uptime"),
    )

    @tool(
        phrases=[
            "сколько работает компьютер",
            "когда я последний раз перезагружался",
            "покажи аптайм",
        ],
        reversible=True,
    )
    async def get_uptime(self) -> ToolResult:
        """Сообщает время работы компьютера с момента последней перезагрузки.

        Возвращает длительность работы в секундах, читаемую фразу и момент загрузки.
        """
        if sys.platform not in SUPPORTED_PLATFORMS:
            return ToolResult.failure(
                f"платформа {sys.platform} не поддерживается",
                speech={
                    "ru": "Не умею узнавать время работы на этой системе.",
                    "en": "I cannot read uptime on this system.",
                },
            )
        try:
            seconds = await asyncio.to_thread(_read_uptime_seconds)
        except UPTIME_ERRORS as error:
            return ToolResult.failure(
                f"не удалось прочитать время работы: {error}",
                speech={
                    "ru": "Не получилось узнать, сколько работает компьютер.",
                    "en": "I could not read the computer uptime.",
                },
            )
        boot_time = datetime.fromtimestamp(time.time() - seconds)
        human_ru = _describe_ru(seconds)
        return ToolResult.success(
            {
                "uptime_seconds": int(seconds),
                "uptime_human": human_ru,
                "boot_time": boot_time.isoformat(timespec="seconds"),
            },
            speech={
                "ru": f"Компьютер работает {human_ru} без перезагрузки.",
                "en": f"The computer has been up for {_describe_en(seconds)}.",
            },
        )

    async def health(self) -> HealthStatus:
        """Проверяет, доступен ли источник данных о времени работы системы."""
        if sys.platform not in SUPPORTED_PLATFORMS:
            return HealthStatus.degraded(f"платформа {sys.platform} не поддерживается")
        try:
            await asyncio.to_thread(_read_uptime_seconds)
        except UPTIME_ERRORS as error:
            return HealthStatus.degraded(f"время работы недоступно: {error}")
        return HealthStatus.healthy()
