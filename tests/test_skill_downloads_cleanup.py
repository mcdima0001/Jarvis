"""Уборка загрузок: только в корзину, только старое и только после «да».

Аудит 01.10.2026 нашёл, что «почисти загрузки» стирала файлы навсегда, сразу и
по времени изменения: распакованное минуту назад считалось старым, а вместе со
старым уходил desktop.ini, дающий папке русское имя. Инструмент при этом ни разу
не вызывался в тестах — ошибка знака в сравнении возраста не роняла ни одного.

Настоящая корзина тут не трогается: функция удаления подменяется переносом
файла в соседний каталог. Часы тоже подменяются — на Windows время создания
файла равно «сейчас», и состарить файл одним `os.utime` нельзя.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jarvis.core.bus import LocalEventBus
from jarvis.core.contracts import ToolResult, Utterance
from jarvis.core.router import Dispatcher, PhraseResolver, Router
from jarvis.core.tools import ToolRegistry, collect_tools

_ROOT = Path(__file__).resolve().parent.parent


def _load() -> Any:
    path = _ROOT / "skills" / "downloads_cleanup" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_downloads_cleanup", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cleanup = _load()


@dataclass
class Box:
    """Папка загрузок, подменная корзина и «сейчас» по часам скилла."""

    folder: Path
    trash: Path
    now: float

    def file(self, name: str, *, fresh: bool = False, size: int = 10) -> Path:
        """Положить файл: по умолчанию старый — ему сорок дней по часам скилла."""
        path = self.folder / name
        path.write_bytes(b"x" * size)
        if fresh:
            os.utime(path, (self.now, self.now))
        return path

    def trashed(self) -> list[str]:
        """Что лежит в корзине."""
        return sorted(item.name for item in self.trash.iterdir())


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Box:
    """Загрузки во временном каталоге и корзина, которая просто переносит файл."""
    folder = tmp_path / "Загрузки"
    folder.mkdir()
    trash = tmp_path / "корзина"
    trash.mkdir()
    later = time.time() + 40 * cleanup.DAY_SECONDS
    monkeypatch.setenv(cleanup.DOWNLOAD_DIR_ENV, str(folder))
    monkeypatch.setattr(cleanup, "_now", lambda: later)
    monkeypatch.setattr(cleanup, "_send_to_trash", lambda path: path.rename(trash / path.name))
    return Box(folder=folder, trash=trash, now=later)


def _dispatcher(events: LocalEventBus, skill: Any) -> Dispatcher:
    """Диспетчер со скиллом и обычным разбором по фразам."""
    registry = ToolRegistry(events=events, default_timeout=5.0)
    for item in collect_tools(skill, namespace="downloads_cleanup"):
        registry.register(item)
    router = Router([PhraseResolver(registry)], threshold=0.6)
    return Dispatcher(router=router, registry=registry, events=events)


def _said(text: str) -> Utterance:
    return Utterance(text=text, source="text")


def _ru(result: ToolResult) -> str:
    text = result.speech_for("ru")
    assert text is not None
    return text


# --- без «да» ничего не трогается ---------------------------------------------


async def test_cleanup_asks_first_and_trashes_only_old_files_on_yes(
    events: LocalEventBus, box: Box
) -> None:
    """«Почисти загрузки» сперва спрашивает; на «да» в корзину уходит только старое.

    Мутация аудита (`<` на `>` в сравнении возраста) удаляла свежее вместо
    старого — здесь она покраснеет.
    """
    old = box.file("старый.zip")
    fresh = box.file("свежий.pdf", fresh=True)
    dispatcher = _dispatcher(events, cleanup.DownloadsCleanupSkill())

    asked = await dispatcher.handle(_said("почисти загрузки"))

    assert asked.confirm is not None
    assert old.exists() and fresh.exists()
    assert box.trashed() == []
    assert "Нашёл 1 файл" in _ru(asked)
    assert _ru(asked).endswith("Отправить в корзину?")

    done = await dispatcher.handle(_said("да"))

    assert done.ok
    assert box.trashed() == ["старый.zip"]
    assert fresh.exists()


async def test_no_keeps_every_file(events: LocalEventBus, box: Box) -> None:
    """«Нет» снимает вопрос, и ни один файл не двигается."""
    old = box.file("старый.zip")
    dispatcher = _dispatcher(events, cleanup.DownloadsCleanupSkill())

    await dispatcher.handle(_said("почисти загрузки"))
    await dispatcher.handle(_said("нет"))

    assert old.exists()
    assert box.trashed() == []


async def test_direct_call_only_asks(box: Box) -> None:
    """Даже прямой вызов без метки вопроса ничего не удаляет, а спрашивает.

    Прямая команда голосом обычно сама себе разрешение, но не здесь: ослышка
    «почисти закладки» и «почисти загрузки в браузере» доходили до этого
    инструмента, а цена ошибки — сотни файлов владельца.
    """
    old = box.file("старый.zip")
    skill = cleanup.DownloadsCleanupSkill()

    result = await skill.delete_old_downloads()

    assert result.ok and result.confirm is not None
    assert result.confirm.tool == "downloads_cleanup.delete_old_downloads"
    assert old.exists()


async def test_forged_or_stale_mark_deletes_nothing(box: Box) -> None:
    """Метку подтверждения нельзя придумать: чужая — отказ, а не уборка."""
    old = box.file("старый.zip")
    skill = cleanup.DownloadsCleanupSkill()

    forged = await skill.delete_old_downloads(batch="подделка")
    assert not forged.ok
    assert old.exists()

    asked = await skill.delete_old_downloads()
    assert asked.confirm is not None
    wrong = await skill.delete_old_downloads(batch="не та метка")
    assert not wrong.ok
    assert old.exists()
    # Вопрос, по которому промахнулись, сгорает: настоящая метка после этого
    # тоже ничего не удалит, нужен новый вопрос.
    late = await skill.delete_old_downloads(**asked.confirm.arguments)
    assert not late.ok
    assert old.exists()


async def test_yes_trashes_only_what_was_counted(events: LocalEventBus, box: Box) -> None:
    """Согласие относится к тому, что было названо в вопросе.

    Файл, появившийся между вопросом и ответом, не входил в «нашёл N файлов»,
    и разрешения на него владелец не давал.
    """
    box.file("старый.zip")
    dispatcher = _dispatcher(events, cleanup.DownloadsCleanupSkill())

    await dispatcher.handle(_said("почисти загрузки"))
    newcomer = box.file("пришёл_потом.iso")
    await dispatcher.handle(_said("да"))

    assert box.trashed() == ["старый.zip"]
    assert newcomer.exists()


# --- что считается старым и что не трогается никогда ------------------------


async def test_explorer_files_are_never_counted(box: Box) -> None:
    """desktop.ini и Thumbs.db не трогаются, как бы стары они ни были.

    desktop.ini даёт папке русское имя «Загрузки»: без него она становится
    Downloads.
    """
    kept = [box.file("desktop.ini"), box.file("Thumbs.db")]
    skill = cleanup.DownloadsCleanupSkill()

    result = await skill.delete_old_downloads()

    assert result.ok and result.confirm is None
    assert all(path.exists() for path in kept)
    assert box.trashed() == []


def test_hidden_and_system_files_are_untouchable() -> None:
    """Скрытое и системное не убирается: это не загрузки владельца, а служебное."""
    plain = Path("отчёт.pdf")
    assert cleanup._untouchable(plain, SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_HIDDEN))
    assert cleanup._untouchable(plain, SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_SYSTEM))
    assert not cleanup._untouchable(plain, SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_ARCHIVE))
    # Где атрибутов Windows нет вовсе, решает только имя.
    assert not cleanup._untouchable(plain, SimpleNamespace())
    assert cleanup._untouchable(Path("DESKTOP.INI"), SimpleNamespace())


@pytest.mark.skipif(sys.platform != "win32", reason="атрибуты файлов есть только в Windows")
async def test_hidden_file_on_disk_is_not_counted(box: Box) -> None:
    """То же на настоящем файле: атрибут читается из `st_file_attributes`."""
    hidden = box.file("служебный.dat")
    kernel32 = ctypes.WinDLL("kernel32")
    assert kernel32.SetFileAttributesW(str(hidden), stat.FILE_ATTRIBUTE_HIDDEN)

    result = await cleanup.DownloadsCleanupSkill().delete_old_downloads()

    assert result.confirm is None
    assert hidden.exists()


def test_age_is_counted_from_the_latest_known_time() -> None:
    """Возраст — по самому позднему из времени создания и изменения.

    Распаковка архива и копирование сохраняют старое время изменения: файл,
    появившийся минуту назад, по нему выглядел годовалым.
    """
    extracted = SimpleNamespace(st_mtime=100.0, st_birthtime=5000.0, st_ctime=1.0)
    assert cleanup._arrived(extracted) == 5000.0
    edited = SimpleNamespace(st_mtime=9000.0, st_birthtime=5000.0, st_ctime=1.0)
    assert cleanup._arrived(edited) == 9000.0


def test_windows_without_birth_time_falls_back_to_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Где `st_birthtime` нет, на Windows время создания лежит в `st_ctime`."""
    monkeypatch.setattr(cleanup.sys, "platform", "win32")
    assert cleanup._arrived(SimpleNamespace(st_mtime=100.0, st_ctime=7000.0)) == 7000.0


@pytest.mark.skipif(sys.platform != "win32", reason="время создания файла есть только в Windows")
def test_just_extracted_file_is_not_old(tmp_path: Path) -> None:
    """Настоящий файл со старой датой изменения, созданный только что, — свежий."""
    path = tmp_path / "из_архива_только_что.docx"
    path.write_bytes(b"x")
    year_ago = time.time() - 400 * cleanup.DAY_SECONDS
    os.utime(path, (year_ago, year_ago))

    assert cleanup._collect_old_files(tmp_path, 30) == []


# --- корзина, а не стирание -------------------------------------------------


def test_outside_windows_nothing_is_erased(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Нет корзины — нет удаления: запасного пути через `unlink` быть не должно."""
    path = tmp_path / "файл.txt"
    path.write_bytes(b"x")
    monkeypatch.setattr(cleanup.sys, "platform", "linux")

    with pytest.raises(OSError):
        cleanup._send_to_trash(path)
    assert path.exists()


def test_shell_operation_layout_matches_windows() -> None:
    """Структура для SHFileOperationW совпадает с той, что ждёт Windows x64.

    Съехавшее поле флагов — это потерянный FOF_ALLOWUNDO, то есть удаление
    мимо корзины. Проверка арифметическая, сама функция не зовётся.
    """
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("раскладка проверяется для 64 бит")
    layout = cleanup._FileOperation
    assert ctypes.sizeof(layout) == 56
    assert layout.fFlags.offset == 32
    assert layout.pFrom.offset == 16
    assert cleanup._TRASH_FLAGS & cleanup.FOF_ALLOWUNDO
    # Файл, который в корзину не влезает, без этого флага Windows стирает молча.
    assert cleanup._TRASH_FLAGS & cleanup.FOF_WANTNUKEWARNING


# --- что звучит -------------------------------------------------------------


async def test_replies_agree_with_numbers(events: LocalEventBus, box: Box) -> None:
    """Числа согласованы, а в русской реплике нет ни одной латинской буквы."""
    for name in ("а.zip", "б.zip", "в.zip"):
        box.file(name)
    skill = cleanup.DownloadsCleanupSkill()

    asked = await skill.delete_old_downloads(days=21)
    question = _ru(asked)
    assert question.startswith("Нашёл 3 файла на 30 байт старше 21 дня.")
    assert not re.search(r"[A-Za-z]", question)

    done = await skill.delete_old_downloads(**asked.confirm.arguments)
    assert _ru(done) == "Отправил в корзину 3 файла, всего 30 байт."
    assert not re.search(r"[A-Za-z]", _ru(done))


async def test_partial_failure_is_named(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    """Не поддавшийся файл называется числом, остальные уходят в корзину."""
    box.file("занят.zip")
    box.file("свободен.zip")

    def picky(path: Path) -> None:
        if path.name == "занят.zip":
            raise OSError("файл открыт в другой программе")
        path.rename(box.trash / path.name)

    monkeypatch.setattr(cleanup, "_send_to_trash", picky)
    skill = cleanup.DownloadsCleanupSkill()
    asked = await skill.delete_old_downloads()
    done = await skill.delete_old_downloads(**asked.confirm.arguments)

    assert done.ok
    assert box.trashed() == ["свободен.zip"]
    assert _ru(done).endswith("но 1 файл не поддался.")


async def test_listing_counts_without_touching(box: Box) -> None:
    """«Покажи старые файлы в загрузках» только считает."""
    box.file("старый.zip")
    box.file("свежий.pdf", fresh=True)
    box.file("desktop.ini")

    result = await cleanup.DownloadsCleanupSkill().list_old_downloads()

    assert result.ok
    assert result.value["files"] == ["старый.zip"]
    assert _ru(result) == "В загрузках 1 файл старше 30 дней, всего 10 байт."
    assert box.trashed() == []
