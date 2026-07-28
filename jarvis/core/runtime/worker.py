"""Выполнение блокирующего кода вне event loop.

faster-whisper и Piper — синхронные и CPU-bound. Если вызвать их прямо в
корутине, встанет **всё** приложение: подписки на события, таймеры, сеть.
Объявить метод `async` при этом недостаточно — асинхронность не появляется
от ключевого слова.

Поэтому в архитектуре есть явный контракт: тяжёлый адаптер ходит только
через `BlockingWorker`. Очередь ограничена семафором, чтобы всплеск запросов
не съел память.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class BlockingWorker:
    """Пул потоков для синхронных задач с ограниченной очередью."""

    def __init__(self, threads: int = 2, *, name: str = "jarvis-worker") -> None:
        self._threads = max(1, threads)
        self._name = name
        self._executor: ThreadPoolExecutor | None = None
        # Пропускаем не больше, чем потоков плюс небольшой буфер: лишние
        # запросы ждут здесь, а не копятся внутри пула.
        self._slots = asyncio.Semaphore(self._threads * 2)

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "worker"

    async def start(self) -> None:
        """Создать пул потоков."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self._threads,
                thread_name_prefix=self._name,
            )
            logger.debug("Пул блокирующих задач поднят: %d поток(ов)", self._threads)

    async def stop(self) -> None:
        """Дождаться текущих задач и закрыть пул."""
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None
            logger.debug("Пул блокирующих задач остановлен")

    async def run(self, func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """Выполнить синхронную функцию в пуле, не блокируя event loop."""
        if self._executor is None:
            raise RuntimeError("BlockingWorker не запущен — вызови start()")
        loop = asyncio.get_running_loop()
        call = functools.partial(func, *args, **kwargs)
        async with self._slots:
            return await loop.run_in_executor(self._executor, call)
