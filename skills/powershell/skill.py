"""Скилл для наблюдения за буфером обмена."""

from __future__ import annotations

import asyncio
import contextlib
import platform
import shutil
import subprocess
import time
from collections import deque
from dataclasses import dataclass

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool

# Сколько записей храним и как часто опрашиваем буфер.
_MAX_HISTORY = 30
_POLL_INTERVAL = 1.5
_MIN_INTERVAL = 0.5
_MAX_INTERVAL = 60.0
_TIMEOUT = 5.0
_PREVIEW_LIMIT = 120


@dataclass(frozen=True)
class _Backend:
    """Пара команд для чтения и записи буфера обмена."""

    name: str
    read_cmd: list[str]
    write_cmd: list[str]
    strip_tail: bool = False


@dataclass(frozen=True)
class _Entry:
    """Одна запомненная копия из буфера обмена."""

    text: str
    at: float


def _detect_backend() -> _Backend | None:
    """Подбирает способ работы с буфером обмена под текущую систему."""
    system = platform.system()
    if system == "Windows":
        powershell = ["powershell", "-NoProfile", "-Command"]
        return _Backend(
            name="powershell",
            read_cmd=[*powershell, "Get-Clipboard -Raw"],
            write_cmd=[*powershell, "$input | Set-Clipboard"],
            strip_tail=True,
        )
    if system == "Darwin":
        return _Backend(name="pbpaste", read_cmd=["pbpaste"], write_cmd=["pbcopy"])
    if shutil.which("wl-paste") and shutil.which("wl-copy"):
        return _Backend(
            name="wl-clipboard",
            read_cmd=["wl-paste", "--no-newline"],
            write_cmd=["wl-copy"],
        )
    if shutil.which("xclip"):
        return _Backend(
            name="xclip",
            read_cmd=["xclip", "-selection", "clipboard", "-o"],
            write_cmd=["xclip", "-selection", "clipboard", "-i"],
        )
    if shutil.which("xsel"):
        return _Backend(
            name="xsel",
            read_cmd=["xsel", "--clipboard", "--output"],
            write_cmd=["xsel", "--clipboard", "--input"],
        )
    return None


def _read_sync(backend: _Backend) -> str:
    """Читает буфер обмена. Блокирующий вызов, только из отдельного потока."""
    proc = subprocess.run(  # noqa: S603
        backend.read_cmd,
        capture_output=True,
        timeout=_TIMEOUT,
        check=False,
    )
    if proc.returncode != 0:
        # Пустой буфер некоторые утилиты отдают как ошибку, это не беда.
        message = proc.stderr.decode("utf-8", "replace").strip().lower()
        if "empty" in message or "owner" in message or not message:
            return ""
        raise OSError(message)
    text = proc.stdout.decode("utf-8", "replace")
    if backend.strip_tail:
        text = text.rstrip("\r\n")
    return text


def _write_sync(backend: _Backend, text: str) -> None:
    """Кладёт текст в буфер обмена. Блокирующий вызов."""
    try:
        proc = subprocess.run(  # noqa: S603
            backend.write_cmd,
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # xclip удерживает выделение и не завершается сам; текст уже передан.
        return
    if proc.returncode != 0:
        raise OSError(proc.stderr.decode("utf-8", "replace").strip())


def _preview(text: str) -> str:
    """Готовит короткий однострочный пересказ содержимого для речи."""
    flat = " ".join(text.split())
    if not flat:
        return "пусто"
    if len(flat) > _PREVIEW_LIMIT:
        return flat[:_PREVIEW_LIMIT].rstrip() + "…"
    return flat


class ClipboardSkill(Skill):
    """Смотрит, что лежит в буфере обмена, и запоминает изменения."""

    meta = SkillMeta(
        name="clipboard",
        description="Читает буфер обмена, следит за изменениями и ведёт историю.",
        version="0.1.0",
        spoken=("буфер обмена", "clipboard"),
    )

    def __init__(self) -> None:
        """Готовит историю и место под фоновое наблюдение."""
        super().__init__()
        self._backend: _Backend | None = _detect_backend()
        self._history: deque[_Entry] = deque(maxlen=_MAX_HISTORY)
        self._watch_task: asyncio.Task[None] | None = None
        self._watch_interval: float = _POLL_INTERVAL

    # --- вспомогательное ---------------------------------------------------

    def _unavailable(self) -> ToolResult:
        """Ответ для системы, где буфер обмена недоступен."""
        return ToolResult.failure(
            "нет доступа к буферу обмена",
            speech={
                "ru": "Не вижу буфер обмена: нет подходящей утилиты.",
                "en": "No clipboard access: a helper tool is missing.",
            },
        )

    async def _read(self, backend: _Backend) -> str:
        """Читает буфер, не блокируя голосовой круг."""
        return await asyncio.to_thread(_read_sync, backend)

    def _remember(self, text: str) -> bool:
        """Кладёт текст в историю, если он новый. Возвращает признак новизны."""
        if not text.strip():
            return False
        if self._history and self._history[-1].text == text:
            return False
        self._history.append(_Entry(text=text, at=time.time()))
        return True

    async def _watch_loop(self, interval: float) -> None:
        """Фоновый опрос буфера обмена с заданным шагом."""
        backend = self._backend
        if backend is None:
            return
        while True:
            await asyncio.sleep(interval)
            try:
                text = await self._read(backend)
            except (OSError, subprocess.TimeoutExpired):
                # Разовый сбой утилиты не должен ронять наблюдение.
                continue
            self._remember(text)

    # --- инструменты -------------------------------------------------------

    @tool(phrases=["что в буфере обмена", "прочитай буфер обмена"], reversible=True)
    async def read_clipboard(self) -> ToolResult:
        """Читает текущее содержимое буфера обмена и сообщает его вслух."""
        backend = self._backend
        if backend is None:
            return self._unavailable()
        try:
            text = await self._read(backend)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ToolResult.failure(
                f"не удалось прочитать буфер обмена: {exc}",
                speech={
                    "ru": "Не смог прочитать буфер обмена.",
                    "en": "I could not read the clipboard.",
                },
            )
        self._remember(text)
        if not text.strip():
            return ToolResult.success(
                {"text": "", "length": 0, "empty": True},
                speech={
                    "ru": "Буфер обмена пуст.",
                    "en": "The clipboard is empty.",
                },
            )
        preview = _preview(text)
        return ToolResult.success(
            {"text": text, "length": len(text), "empty": False},
            speech={
                "ru": f"В буфере обмена: {preview}",
                "en": f"Clipboard holds: {preview}",
            },
        )

    @tool(phrases=["следи за буфером обмена", "начни смотреть буфер"], reversible=True)
    async def watch_clipboard(self, interval: float = _POLL_INTERVAL) -> ToolResult:
        """Включает фоновое наблюдение за буфером обмена.

        :param interval: шаг опроса в секундах.
        """
        backend = self._backend
        if backend is None:
            return self._unavailable()
        step = min(max(float(interval), _MIN_INTERVAL), _MAX_INTERVAL)
        if self._watch_task is not None and not self._watch_task.done():
            return ToolResult.success(
                {"watching": True, "interval": self._watch_interval},
                speech={
                    "ru": "Уже слежу за буфером обмена.",
                    "en": "I am already watching the clipboard.",
                },
            )
        try:
            current = await self._read(backend)
        except (OSError, subprocess.TimeoutExpired):
            current = ""
        self._remember(current)
        self._watch_interval = step
        self._watch_task = asyncio.create_task(self._watch_loop(step))
        return ToolResult.success(
            {"watching": True, "interval": step},
            speech={
                "ru": f"Слежу за буфером обмена, проверяю раз в {step:.0f} секунд.",
                "en": f"Watching the clipboard every {step:.0f} seconds.",
            },
        )

    @tool(
        phrases=["хватит следить за буфером", "останови слежку за буфером"],
        reversible=True,
    )
    async def stop_watching(self) -> ToolResult:
        """Выключает фоновое наблюдение за буфером обмена."""
        task = self._watch_task
        if task is None or task.done():
            self._watch_task = None
            return ToolResult.success(
                {"watching": False},
                speech={
                    "ru": "За буфером обмена я и не следил.",
                    "en": "I was not watching the clipboard.",
                },
            )
        task.cancel()
        # Отмена штатная, ждём только чтобы задача успела закрыться.
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._watch_task = None
        return ToolResult.success(
            {"watching": False, "remembered": len(self._history)},
            speech={
                "ru": "Больше за буфером обмена не слежу.",
                "en": "I stopped watching the clipboard.",
            },
        )

    @tool(phrases=["что копировали", "история буфера обмена"], reversible=True)
    async def clipboard_history(self, limit: int = 5) -> ToolResult:
        """Показывает недавние копии из буфера обмена.

        :param limit: сколько последних записей вернуть.
        """
        count = min(max(int(limit), 1), _MAX_HISTORY)
        items = list(self._history)[-count:]
        payload = [
            {"text": entry.text, "at": entry.at, "preview": _preview(entry.text)}
            for entry in reversed(items)
        ]
        if not payload:
            return ToolResult.success(
                {"items": [], "total": 0},
                speech={
                    "ru": "История буфера обмена пока пуста.",
                    "en": "The clipboard history is empty so far.",
                },
            )
        last = payload[0]["preview"]
        return ToolResult.success(
            {"items": payload, "total": len(self._history)},
            speech={
                "ru": f"Запомнил {len(payload)} записей, последняя: {last}",
                "en": f"I remember {len(payload)} entries, the latest: {last}",
            },
        )

    @tool(phrases=["скопируй в буфер обмена", "положи в буфер"], reversible=True)
    async def put_to_clipboard(self, text: str = "") -> ToolResult:
        """Кладёт указанный текст в буфер обмена.

        :param text: что положить в буфер.
        """
        backend = self._backend
        if backend is None:
            return self._unavailable()
        if not text:
            return ToolResult.failure(
                "нечего класть в буфер обмена",
                speech={
                    "ru": "Скажи, что положить в буфер обмена.",
                    "en": "Tell me what to put on the clipboard.",
                },
            )
        try:
            previous = await self._read(backend)
        except (OSError, subprocess.TimeoutExpired):
            previous = ""
        try:
            await asyncio.to_thread(_write_sync, backend, text)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ToolResult.failure(
                f"не удалось записать буфер обмена: {exc}",
                speech={
                    "ru": "Не смог записать в буфер обмена.",
                    "en": "I could not write to the clipboard.",
                },
            )
        self._remember(previous)
        self._remember(text)
        return ToolResult.success(
            {"text": text, "previous": previous, "length": len(text)},
            speech={
                "ru": f"Положил в буфер обмена: {_preview(text)}",
                "en": f"Copied to the clipboard: {_preview(text)}",
            },
        )

    @tool(phrases=["очисти буфер обмена", "сотри буфер обмена"], reversible=False)
    async def clear_clipboard(self) -> ToolResult:
        """Очищает буфер обмена, прежнее содержимое остаётся только в истории."""
        backend = self._backend
        if backend is None:
            return self._unavailable()
        try:
            previous = await self._read(backend)
        except (OSError, subprocess.TimeoutExpired):
            previous = ""
        self._remember(previous)
        try:
            await asyncio.to_thread(_write_sync, backend, "")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ToolResult.failure(
                f"не удалось очистить буфер обмена: {exc}",
                speech={
                    "ru": "Не смог очистить буфер обмена.",
                    "en": "I could not clear the clipboard.",
                },
            )
        return ToolResult.success(
            {"cleared": True, "previous": previous},
            speech={
                "ru": "Буфер обмена очищен, прежний текст остался в истории.",
                "en": "Clipboard cleared, the old text stays in the history.",
            },
        )

    # --- состояние ---------------------------------------------------------

    async def health_check(self) -> HealthStatus:
        """Проверяет, доступен ли буфер обмена в этой системе."""
        backend = self._backend
        if backend is None:
            return HealthStatus.degraded(
                f"нет утилиты для буфера обмена в системе {platform.system()}"
            )
        try:
            await self._read(backend)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return HealthStatus.degraded(f"{backend.name} не отвечает: {exc}")
        watching = self._watch_task is not None and not self._watch_task.done()
        state = "слежу" if watching else "жду команды"
        return HealthStatus.healthy(f"{backend.name}: {state}")
