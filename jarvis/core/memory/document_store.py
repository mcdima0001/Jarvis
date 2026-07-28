"""Файловое хранилище документов: раздел -> JSON-файл.

Один файл на раздел, а не одна большая свалка: так в контекст модели уезжает
только то, что действительно запросили. Замена бэкенда (SQLite, векторная база)
не заденет скиллы — они зависят от протокола.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Mapping

from jarvis.core.errors import MemoryError_

logger = logging.getLogger(__name__)


class FileDocumentStore:
    """Документы в JSON-файлах внутри одного каталога."""

    def __init__(self, directory: Path, namespaces: tuple[str, ...]) -> None:
        self._dir = directory
        self._namespaces = namespaces
        self._locks: dict[str, asyncio.Lock] = {name: asyncio.Lock() for name in namespaces}
        self._dir.mkdir(parents=True, exist_ok=True)

    def namespaces(self) -> tuple[str, ...]:
        """Доступные разделы."""
        return self._namespaces

    def _path(self, namespace: str) -> Path:
        """Путь к файлу раздела с проверкой имени."""
        if namespace not in self._namespaces:
            raise MemoryError_(
                f"Раздел памяти {namespace!r} не объявлен. Доступны: "
                f"{', '.join(self._namespaces) or '(ни одного)'}"
            )
        return self._dir / f"{namespace}.json"

    def _lock(self, namespace: str) -> asyncio.Lock:
        """Блокировка на раздел, чтобы не потерять параллельные правки."""
        return self._locks.setdefault(namespace, asyncio.Lock())

    @staticmethod
    def _read_sync(path: Path) -> dict[str, Any]:
        """Синхронное чтение — выполняется в отдельном потоке."""
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError as exc:
            logger.error("Повреждён файл памяти %s: %s", path, exc)
            return {}

    @staticmethod
    def _write_sync(path: Path, data: Mapping[str, Any]) -> None:
        """Атомарная запись: сначала во временный файл, потом подмена."""
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)

    async def read(self, namespace: str) -> dict[str, Any]:
        """Прочитать раздел целиком."""
        path = self._path(namespace)
        return await asyncio.to_thread(self._read_sync, path)

    async def write(self, namespace: str, data: Mapping[str, Any]) -> None:
        """Полностью заменить содержимое раздела."""
        path = self._path(namespace)
        async with self._lock(namespace):
            await asyncio.to_thread(self._write_sync, path, dict(data))

    async def update(self, namespace: str, values: Mapping[str, Any]) -> dict[str, Any]:
        """Обновить часть ключей раздела и вернуть результат."""
        path = self._path(namespace)
        async with self._lock(namespace):
            current = await asyncio.to_thread(self._read_sync, path)
            current.update(values)
            await asyncio.to_thread(self._write_sync, path, current)
            return current

    async def get(self, namespace: str, key: str, default: Any = None) -> Any:
        """Прочитать одно значение."""
        data = await self.read(namespace)
        return data.get(key, default)

    async def set(self, namespace: str, key: str, value: Any) -> None:
        """Записать одно значение."""
        await self.update(namespace, {key: value})
