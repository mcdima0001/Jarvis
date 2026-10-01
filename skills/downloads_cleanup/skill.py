"""Уборка старых файлов из каталога загрузок — в корзину и только с разрешения.

Аудит 01.10.2026: «почисти загрузки» стирала файлы навсегда, сразу и по времени
изменения. Распакованное минуту назад считалось старым (архив сохраняет даты),
desktop.ini уходил вместе со всем и уносил русское имя папки, а дойти до
инструмента можно было ослышкой («почисти закладки»). Поэтому теперь три правила:

* **корзина, а не стирание** — ошибку можно исправить из проводника;
* **старое — по самому позднему из времени создания и изменения**, служебное и
  скрытое не трогается вовсе;
* **сначала вопрос, потом уборка**, даже на прямую команду: инструмент считает,
  называет число и объём и ждёт «да».
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import secrets
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from jarvis.core.contracts import Intent, ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool
from jarvis.core.tts.normalize import plural_form

logger = logging.getLogger(__name__)

# Возможные имена каталога загрузок в разных локалях
DOWNLOAD_DIR_NAMES = ("Downloads", "Загрузки", "Download")

# Переменная окружения, которой можно переопределить каталог
DOWNLOAD_DIR_ENV = "JARVIS_DOWNLOADS_DIR"

# Сколько имён файлов класть в полезную нагрузку
MAX_LISTED = 50

# Сколько секунд в сутках
DAY_SECONDS = 86400.0

#: Служебные файлы проводника. Не загрузки владельца, как бы стары они ни были:
#: desktop.ini даёт папке русское имя «Загрузки» — без него она станет Downloads.
KEEP_NAMES = frozenset({"desktop.ini", "thumbs.db"})

#: Скрытое и системное кладёт туда не владелец, и убирать это не ему.
KEEP_ATTRIBUTES = stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM

#: Предел ожидания уборки. Файлы уходят в корзину по одному (сбой одного не
#: обрывает остальные), и две сотни штук занимают секунды, а не доли секунды.
TRASH_TIMEOUT_S = 120.0

# SHFileOperationW из shellapi.h
FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000

#: С FOF_ALLOWUNDO Windows кладёт файл в корзину, **если может**. Не может
#: (файл больше корзины, корзина на диске выключена) — с FOF_NOCONFIRMATION она
#: молча стирает его насовсем. FOF_WANTNUKEWARNING возвращает на этот случай
#: системный вопрос: лучше окно «удалить безвозвратно?», чем тихая потеря.
_TRASH_FLAGS = (
    FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING
)

_FILES = ("файл", "файла", "файлов")
#: После «старше»: старше одного дня, двух дней, тридцати дней.
_DAYS_AFTER_OLDER = ("дня", "дней", "дней")
_UNITS_RU = (
    ("байт", "байта", "байт"),
    ("килобайт", "килобайта", "килобайт"),
    ("мегабайт", "мегабайта", "мегабайт"),
    ("гигабайт", "гигабайта", "гигабайт"),
)
_UNITS_EN = ("byte", "kilobyte", "megabyte", "gigabyte")


class _FileOperation(ctypes.Structure):
    """SHFILEOPSTRUCTW. Раскладка проверяется тестом: съехавшее поле флагов —
    это потерянный FOF_ALLOWUNDO, то есть удаление мимо корзины."""

    # В 32-битной сборке shellapi.h пакует структуру по байту, в 64-битной
    # выравнивание естественное.
    if ctypes.sizeof(ctypes.c_void_p) == 4:
        _pack_ = 1
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_void_p),
        ("pTo", ctypes.c_void_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_void_p),
    ]


@dataclass(frozen=True, slots=True)
class _Question:
    """Заданный вопрос: «да» разрешает ровно то, что в нём названо."""

    mark: str
    directory: Path
    days: int
    files: frozenset[Path]


def _now() -> float:
    """Сейчас по стенным часам.

    Отдельной функцией ради тестов: на Windows время создания файла — всегда
    «только что», и состарить файл одним `os.utime` нельзя.
    """
    return time.time()


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


def _arrived(stats: os.stat_result) -> float:
    """Когда файл появился в папке: самое позднее из создания и изменения.

    Одного времени изменения мало: распаковка архива и копирование сохраняют
    старую дату, и файл, появившийся минуту назад, выглядел годовалым.
    """
    born = getattr(stats, "st_birthtime", None)
    if born is None and sys.platform == "win32":
        # До Python 3.12 время создания на Windows лежало в st_ctime.
        born = stats.st_ctime
    return stats.st_mtime if born is None else max(stats.st_mtime, born)


def _untouchable(path: Path, stats: os.stat_result) -> bool:
    """Служебный ли это файл, который уборка не трогает никогда."""
    if path.name.lower() in KEEP_NAMES:
        return True
    return bool(getattr(stats, "st_file_attributes", 0) & KEEP_ATTRIBUTES)


def _collect_old_files(directory: Path, days: int) -> list[tuple[Path, int]]:
    """Собирает обычные файлы старше указанного числа суток.

    Каталоги, символические ссылки, недоступные записи, скрытое и системное
    пропускаются: удалять их вслепую по голосовой команде слишком опасно.
    """
    threshold = _now() - days * DAY_SECONDS
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
        if _untouchable(entry, stats):
            continue
        if _arrived(stats) < threshold:
            found.append((entry, stats.st_size))
    return sorted(found, key=lambda item: item[0].name.lower())


def _send_to_trash(path: Path) -> None:
    """Отправить файл в корзину Windows. Не вышло — `OSError`, а файл на месте.

    Запасного пути через `unlink` нет намеренно: без корзины уборка не делается
    вовсе. Об успехе судим по тому, исчез ли файл, а не по коду возврата.
    """
    if sys.platform != "win32":
        raise OSError("корзина есть только в Windows")
    # Без полного пути Windows FOF_ALLOWUNDO молча игнорирует — и стирает насовсем.
    target = path.resolve()
    # pFrom — список путей, закрытый двумя нулями. Строку с нулём внутри ctypes
    # не примет, поэтому второй ноль даёт буфер на знак длиннее.
    single = ctypes.create_unicode_buffer(str(target))
    names = ctypes.create_unicode_buffer(single.value, len(single) + 1)
    operation = _FileOperation(
        wFunc=FO_DELETE, pFrom=ctypes.addressof(names), fFlags=_TRASH_FLAGS
    )
    shell32 = ctypes.WinDLL("shell32")
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_FileOperation)]
    shell32.SHFileOperationW.restype = ctypes.c_int
    code = shell32.SHFileOperationW(ctypes.byref(operation))
    if target.exists():
        raise OSError(f"файл остался на месте, код {code:#x}")


def _trash_files(paths: list[Path]) -> tuple[list[str], int, list[str]]:
    """Отправляет файлы в корзину по одному.

    :return: имена убранных, их общий объём и ошибки по неподдавшимся.
    """
    moved: list[str] = []
    failed: list[str] = []
    total = 0
    for path in paths:
        try:
            size = path.stat().st_size
            _send_to_trash(path)
        except OSError as error:
            failed.append(f"{path.name}: {error.strerror or error}")
            continue
        moved.append(path.name)
        total += size
    return moved, total, failed


def _scaled(size: int) -> tuple[int, int]:
    """Объём целым числом и номер единицы: байты, килобайты, мега-, гига-."""
    value = float(size)
    unit = 0
    while value >= 1024.0 and unit < len(_UNITS_EN) - 1:
        value /= 1024.0
        unit += 1
    return round(value), unit


def _size_ru(size: int) -> str:
    """Объём для русской реплики: «3 гигабайта», «30 байт»."""
    whole, unit = _scaled(size)
    return f"{whole} {plural_form(whole, _UNITS_RU[unit])}"


def _size_en(size: int) -> str:
    """Объём для английской реплики, словами, без сокращений."""
    whole, unit = _scaled(size)
    return f"{whole} {_UNITS_EN[unit]}{'' if whole == 1 else 's'}"


def _older_ru(days: int) -> str:
    """«старше 30 дней», «старше 21 дня»."""
    return f"старше {days} {plural_form(days, _DAYS_AFTER_OLDER)}"


def _older_en(days: int) -> str:
    """«older than 30 days», «older than 1 day»."""
    return f"older than {days} day{'' if days == 1 else 's'}"


def _files_ru(count: int) -> str:
    """«1 файл», «3 файла», «5 файлов»."""
    return f"{count} {plural_form(count, _FILES)}"


def _files_en(count: int) -> str:
    """«1 file», «3 files»."""
    return f"{count} file{'' if count == 1 else 's'}"


class DownloadsCleanupSkill(Skill):
    """Показывает давно залежавшиеся файлы в загрузках и убирает их в корзину."""

    meta = SkillMeta(
        name="downloads_cleanup",
        description="Находит старые файлы в каталоге загрузок и отправляет их в корзину.",
        version="0.2.0",
        spoken=("уборка загрузок", "downloads cleanup"),
    )

    def __init__(self) -> None:
        super().__init__()
        #: Последний заданный вопрос. Живёт в памяти и не переживает ни
        #: перезапуска, ни переподключения модуля: «да» на вопрос, которого
        #: скилл уже не помнит, ничего не удаляет.
        self._question: _Question | None = None

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
        reversible=True, routable=False)
    async def list_old_downloads(self, days: int = 30) -> ToolResult:
        """Перечисляет файлы в каталоге загрузок старше указанного числа дней.

        :param days: возраст файла в сутках, начиная с которого он считается старым.
        """
        if days < 1:
            return _too_young()
        directory = await asyncio.to_thread(_find_downloads_dir)
        if directory is None:
            return _no_directory()
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
                    "ru": f"В загрузках нет файлов {_older_ru(days)}.",
                    "en": f"No downloads are {_older_en(days)}.",
                },
            )
        return ToolResult.success(
            payload,
            speech={
                "ru": (
                    f"В загрузках {_files_ru(count)} {_older_ru(days)}, "
                    f"всего {_size_ru(total_size)}."
                ),
                "en": (
                    f"There are {_files_en(count)} {_older_en(days)} in downloads, "
                    f"{_size_en(total_size)} in total."
                ),
            },
        )

    @tool(
        phrases=[
            "удали старые файлы из загрузок",
            "почисти загрузки",
        ],
        reversible=False, routable=False, timeout=TRASH_TIMEOUT_S)
    async def delete_old_downloads(self, days: int = 30, batch: str = "") -> ToolResult:
        """Отправляет в корзину файлы из загрузок старше указанного числа дней, спросив разрешения.

        Без метки инструмент только считает и спрашивает — и на прямую команду
        тоже: до него доходили ослышки («почисти закладки») и фразы с хвостом
        («почисти загрузки в браузере»), а цена ошибки — сотни файлов владельца.
        Убирать файлы начинает только «да»: диспетчер повторяет вызов с меткой
        вопроса.

        :param days: возраст файла в сутках, начиная с которого он убирается.
        :param batch: служебная метка вопроса, на который владелец ответил «да».
        """
        if days < 1:
            return _too_young()
        directory = await asyncio.to_thread(_find_downloads_dir)
        if directory is None:
            return _no_directory()
        if batch:
            return await self._trash_counted(directory, days, batch)

        found = await asyncio.to_thread(_collect_old_files, directory, days)
        if not found:
            return ToolResult.success(
                {"directory": str(directory), "days": days, "count": 0, "total_bytes": 0},
                speech={
                    "ru": f"Удалять нечего, файлов {_older_ru(days)} нет.",
                    "en": f"Nothing to clean up, no files are {_older_en(days)}.",
                },
            )
        count = len(found)
        total_size = sum(size for _, size in found)
        # Метка случайная: придумать её нельзя ни модели, ни выученному, ни
        # плану — знает её только диспетчер, запомнивший вопрос.
        mark = secrets.token_hex(8)
        self._question = _Question(
            mark=mark, directory=directory, days=days,
            files=frozenset(path for path, _ in found),
        )
        return ToolResult.asking(
            Intent(
                tool=f"{self.meta.name}.delete_old_downloads",
                arguments={"days": days, "batch": mark},
            ),
            question={
                "ru": (
                    f"Нашёл {_files_ru(count)} на {_size_ru(total_size)} "
                    f"{_older_ru(days)}. Отправить в корзину?"
                ),
                "en": (
                    f"I found {_files_en(count)} {_older_en(days)}, "
                    f"{_size_en(total_size)} in total. Move them to the recycle bin?"
                ),
            },
            value={
                "directory": str(directory),
                "days": days,
                "count": count,
                "total_bytes": total_size,
                "files": [path.name for path, _ in found[:MAX_LISTED]],
            },
        )

    async def _trash_counted(self, directory: Path, days: int, batch: str) -> ToolResult:
        """Убрать в корзину то, что было названо в вопросе, — и только это."""
        # Вопрос сгорает при любой попытке: вторая с той же меткой ничего не тронет.
        question, self._question = self._question, None
        if (
            question is None
            or question.mark != batch
            or question.days != days
            or question.directory != directory
        ):
            return ToolResult.failure(
                "Метка подтверждения не совпала с заданным вопросом.",
                speech={
                    "ru": "Вопрос уже устарел, ничего не удалил. Попроси почистить загрузки ещё раз.",
                    "en": "That question has expired, nothing was removed. Ask me again.",
                },
            )
        # Пересчёт, а не список из вопроса как есть: файл, который с тех пор
        # открыли и сохранили, перестал быть старым. А новые старые в «нашёл N
        # файлов» не входили, и разрешения на них не было.
        current = await asyncio.to_thread(_collect_old_files, directory, days)
        targets = [path for path, _ in current if path in question.files]
        if not targets:
            return ToolResult.success(
                {"directory": str(directory), "days": days, "trashed_count": 0},
                speech={
                    "ru": "Убирать уже нечего: названные файлы исчезли или изменились.",
                    "en": "Nothing left to move: those files are gone or have changed.",
                },
            )
        logger.info("Отправляю в корзину %d файлов из %s", len(targets), directory)
        moved, total_size, failed = await asyncio.to_thread(_trash_files, targets)
        for problem in failed:
            logger.warning("Не ушёл в корзину: %s", problem)
        count = len(moved)
        if count == 0:
            return ToolResult.failure(
                "Ни один файл не удалось отправить в корзину.",
                speech={
                    "ru": "Не смог отправить в корзину ни одного файла из загрузок.",
                    "en": "I could not move any of the downloads to the recycle bin.",
                },
            )
        payload = {
            "directory": str(directory),
            "days": days,
            "trashed_count": count,
            "total_bytes": total_size,
            "trashed": moved[:MAX_LISTED],
            "failed": failed[:MAX_LISTED],
        }
        done_ru = f"Отправил в корзину {_files_ru(count)}, всего {_size_ru(total_size)}"
        done_en = (
            f"Moved {_files_en(count)} to the recycle bin, {_size_en(total_size)} in total"
        )
        if failed:
            skipped = len(failed)
            stuck = plural_form(
                skipped, ("файл не поддался", "файла не поддались", "файлов не поддались")
            )
            return ToolResult.success(
                payload,
                speech={
                    "ru": f"{done_ru}, но {skipped} {stuck}.",
                    "en": f"{done_en}, but {skipped} could not be moved.",
                },
            )
        return ToolResult.success(payload, speech={"ru": f"{done_ru}.", "en": f"{done_en}."})


def _too_young() -> ToolResult:
    """Отказ на возраст меньше суток."""
    return ToolResult.failure(
        "Возраст файлов должен быть не меньше одного дня.",
        speech={
            "ru": "Скажите возраст файлов хотя бы в один день.",
            "en": "Please give an age of at least one day.",
        },
    )


def _no_directory() -> ToolResult:
    """Отказ, когда каталога загрузок нет."""
    return ToolResult.failure(
        "Каталог загрузок не найден.",
        speech={
            "ru": "Не нашёл каталог загрузок.",
            "en": "I could not find the downloads folder.",
        },
    )
