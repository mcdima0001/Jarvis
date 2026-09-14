"""Удаление старых файлов из каталога загрузок."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool

# Возможные имена каталога загрузок в разных локалях
DOWNLOAD_DIR_NAMES = ("Downloads", "Загрузки", "Download")

# Переменная окружения, которой можно переопределить каталог
DOWNLOAD_DIR_ENV = "JARVIS_DOWNLOADS_DIR"

# Сколько имён файлов класть в полезную нагрузку
MAX_LISTED = 50

# Сколько секунд в сутках
DAY_SECONDS = 86400.0


def _find_downloads_dir() -> Path | None:
    """Ищет каталог загрузок пользователя, возвращает None, если его нет."""
    override = os.environ.get(DOWNLOAD_DIR_ENV, "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_dir() else None
    home = Path.home()
    for name in DOWNLOAD_DIR_NAMES:
        candidate = home / name
        if candidate.is_dir():
            return candidate
    return None


def _collect_old_files(directory: Path, days: int) -> list[tuple[Path, int]]:
    """Собирает обычные файлы старше указанного числа суток.

    Каталоги, символические ссылки и недоступные записи пропускаются:
    удалять их вслепую по голосовой команде слишком опасно.
    """
    threshold = time.time() - days * DAY_SECONDS
    found: list[tuple[Path, int]] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return found
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            continue
        try:
            stats = entry.stat()
        except OSError:
            continue
        if stats.st_mtime < threshold:
            found.append((entry, stats.st_size))
    return sorted(found, key=lambda item: item[0].name.lower())


def _delete_files(paths: list[Path]) -> tuple[list[str], int, list[str]]:
    """Удаляет файлы и возвращает имена удалённых, освобождённый объём и ошибки."""
    deleted: list[str] = []
    failed: list[str] = []
    freed = 0
    for path in paths:
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError as error:
            failed.append(f"{path.name}: {error.strerror or error}")
            continue
        deleted.append(path.name)
        freed += size
    return deleted, freed, failed


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Подбирает русскую форму слова для числа."""
    if count % 100 in range(11, 15):
        return many
    if count % 10 == 1:
        return one
    if count % 10 in (2, 3, 4):
        return few
    return many


def _format_size_ru(size: int) -> str:
    """Переводит байты в короткую фразу для произношения."""
    value = float(size)
    for unit in ("килобайт", "мегабайт", "гигабайт"):
        value /= 1024.0
        if value < 1024.0 or unit == "гигабайт":
            return f"{value:.0f} {unit}"
    return f"{value:.0f} гигабайт"


def _format_size_en(size: int) -> str:
    """То же самое для английской реплики."""
    value = float(size)
    for unit in ("KB", "MB", "GB"):
        value /= 1024.0
        if value < 1024.0 or unit == "GB":
            return f"{value:.0f} {unit}"
    return f"{value:.0f} GB"


class DownloadsCleanupSkill(Skill):
    """Показывает и удаляет давно залежавшиеся файлы в каталоге загрузок."""

    meta = SkillMeta(
        name="downloads_cleanup",
        description="Находит и удаляет старые файлы в каталоге загрузок.",
        version="0.1.0",
        spoken=("уборка загрузок", "downloads cleanup"),
    )

    async def health(self) -> HealthStatus:
        """Проверяет, что каталог загрузок вообще существует."""
        directory = await asyncio.to_thread(_find_downloads_dir)
        if directory is None:
            return HealthStatus.degraded("Каталог загрузок не найден.")
        return HealthStatus.healthy()

    @tool(
        phrases=[
            "покажи старые файлы в загрузках",
            "что можно удалить из загрузок",
        ],
        reversible=True,
    )
    async def list_old_downloads(self, days: int = 30) -> ToolResult:
        """Перечисляет файлы в каталоге загрузок старше указанного числа дней.

        :param days: возраст файла в сутках, начиная с которого он считается старым.
        """
        if days < 1:
            return ToolResult.failure(
                "Возраст файлов должен быть не меньше одного дня.",
                speech={
                    "ru": "Скажите возраст файлов хотя бы в один день.",
                    "en": "Please give an age of at least one day.",
                },
            )
        directory = await asyncio.to_thread(_find_downloads_dir)
        if directory is None:
            return ToolResult.failure(
                "Каталог загрузок не найден.",
                speech={
                    "ru": "Не нашёл каталог загрузок.",
                    "en": "I could not find the downloads folder.",
                },
            )
        found = await asyncio.to_thread(_collect_old_files, directory, days)
        total_size = sum(size for _, size in found)
        count = len(found)
        payload = {
            "directory": str(directory),
            "days": days,
            "count": count,
            "total_bytes": total_size,
            "files": [path.name for path, _ in found[:MAX_LISTED]],
        }
        if count == 0:
            return ToolResult.success(
                payload,
                speech={
                    "ru": f"В загрузках нет файлов старше {days} дней.",
                    "en": f"No downloads are older than {days} days.",
                },
            )
        word = _plural(count, "файл", "файла", "файлов")
        return ToolResult.success(
            payload,
            speech={
                "ru": (
                    f"В загрузках {count} {word} старше {days} дней, "
                    f"всего {_format_size_ru(total_size)}."
                ),
                "en": (
                    f"There are {count} downloads older than {days} days, "
                    f"{_format_size_en(total_size)} in total."
                ),
            },
        )

    @tool(
        phrases=[
            "удали старые файлы из загрузок",
            "почисти загрузки",
        ],
        reversible=False,
    )
    async def delete_old_downloads(self, days: int = 30) -> ToolResult:
        """Безвозвратно удаляет файлы в каталоге загрузок старше указанного числа дней.

        :param days: возраст файла в сутках, начиная с которого он удаляется.
        """
        if days < 1:
            return ToolResult.failure(
                "Возраст файлов должен быть не меньше одного дня.",
                speech={
                    "ru": "Скажите возраст файлов хотя бы в один день.",
                    "en": "Please give an age of at least one day.",
                },
            )
        directory = await asyncio.to_thread(_find_downloads_dir)
        if directory is None:
            return ToolResult.failure(
                "Каталог загрузок не найден.",
                speech={
                    "ru": "Не нашёл каталог загрузок.",
                    "en": "I could not find the downloads folder.",
                },
            )
        found = await asyncio.to_thread(_collect_old_files, directory, days)
        if not found:
            return ToolResult.success(
                {
                    "directory": str(directory),
                    "days": days,
                    "deleted_count": 0,
                    "freed_bytes": 0,
                    "deleted": [],
                    "failed": [],
                },
                speech={
                    "ru": f"Удалять нечего, файлов старше {days} дней нет.",
                    "en": f"Nothing to delete, no files older than {days} days.",
                },
            )
        deleted, freed, failed = await asyncio.to_thread(
            _delete_files, [path for path, _ in found]
        )
        count = len(deleted)
        payload = {
            "directory": str(directory),
            "days": days,
            "deleted_count": count,
            "freed_bytes": freed,
            "deleted": deleted[:MAX_LISTED],
            "failed": failed[:MAX_LISTED],
        }
        word = _plural(count, "файл", "файла", "файлов")
        if failed and count == 0:
            return ToolResult.failure(
                "Ни один файл не удалось удалить.",
                speech={
                    "ru": "Не смог удалить ни одного файла из загрузок.",
                    "en": "I could not delete any of the downloads.",
                },
            )
        if failed:
            skipped = len(failed)
            return ToolResult.success(
                payload,
                speech={
                    "ru": (
                        f"Удалил {count} {word} и освободил {_format_size_ru(freed)}, "
                        f"но {skipped} не поддались."
                    ),
                    "en": (
                        f"Deleted {count} files and freed {_format_size_en(freed)}, "
                        f"but {skipped} could not be removed."
                    ),
                },
            )
        return ToolResult.success(
            payload,
            speech={
                "ru": (
                    f"Удалил {count} {word} из загрузок и освободил "
                    f"{_format_size_ru(freed)}."
                ),
                "en": (
                    f"Deleted {count} old downloads and freed "
                    f"{_format_size_en(freed)}."
                ),
            },
        )
