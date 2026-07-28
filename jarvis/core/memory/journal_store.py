"""Файловое хранилище журналов: раздел -> JSONL, только добавление.

`today` и `history` — не документы: их не правят, в них дописывают, а читают
по времени. Формат JSONL даёт дешёвое добавление и устойчивость к обрыву записи.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from jarvis.core.errors import MemoryError_

from .protocol import JournalEntry

logger = logging.getLogger(__name__)

#: Сколько последних строк файла читать при выборке.
_TAIL_LINES = 2000


class FileJournalStore:
    """Журналы в JSONL-файлах внутри одного каталога."""

    def __init__(self, directory: Path, namespaces: tuple[str, ...]) -> None:
        self._dir = directory
        self._namespaces = namespaces
        self._locks: dict[str, asyncio.Lock] = {name: asyncio.Lock() for name in namespaces}
        self._dir.mkdir(parents=True, exist_ok=True)

    def namespaces(self) -> tuple[str, ...]:
        """Доступные журналы."""
        return self._namespaces

    def _path(self, namespace: str) -> Path:
        """Путь к файлу журнала с проверкой имени."""
        if namespace not in self._namespaces:
            raise MemoryError_(
                f"Журнал {namespace!r} не объявлен. Доступны: "
                f"{', '.join(self._namespaces) or '(ни одного)'}"
            )
        return self._dir / f"{namespace}.jsonl"

    @staticmethod
    def _append_sync(path: Path, payload: Mapping[str, Any]) -> None:
        """Синхронное добавление строки."""
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    @staticmethod
    def _tail_sync(path: Path, lines: int) -> list[dict[str, Any]]:
        """Прочитать последние строки файла."""
        if not path.is_file():
            return []
        raw = path.read_text(encoding="utf-8").splitlines()[-lines:]
        entries: list[dict[str, Any]] = []
        for line in raw:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Пропущена повреждённая строка журнала в %s", path)
        return entries

    async def append(
        self,
        namespace: str,
        text: str,
        *,
        tags: Sequence[str] = (),
        data: Mapping[str, Any] | None = None,
    ) -> JournalEntry:
        """Добавить запись в журнал."""
        entry = JournalEntry(
            timestamp=time.time(),
            text=text,
            tags=tuple(tags),
            data=dict(data or {}),
        )
        path = self._path(namespace)
        payload = {
            "timestamp": entry.timestamp,
            "text": entry.text,
            "tags": list(entry.tags),
            "data": dict(entry.data),
        }
        async with self._locks.setdefault(namespace, asyncio.Lock()):
            await asyncio.to_thread(self._append_sync, path, payload)
        return entry

    async def recent(
        self,
        namespace: str,
        *,
        limit: int = 20,
        since: float | None = None,
        tag: str | None = None,
    ) -> list[JournalEntry]:
        """Последние записи журнала с фильтрами по времени и тегу."""
        path = self._path(namespace)
        raw = await asyncio.to_thread(self._tail_sync, path, _TAIL_LINES)

        entries: list[JournalEntry] = []
        for item in raw:
            timestamp = float(item.get("timestamp", 0.0))
            if since is not None and timestamp < since:
                continue
            tags = tuple(item.get("tags") or ())
            if tag is not None and tag not in tags:
                continue
            entries.append(
                JournalEntry(
                    timestamp=timestamp,
                    text=str(item.get("text", "")),
                    tags=tags,
                    data=dict(item.get("data") or {}),
                )
            )
        return entries[-limit:]
