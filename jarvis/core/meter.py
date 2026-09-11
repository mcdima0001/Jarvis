"""Кто именно греет ноутбук: учёт процессорного времени по звеньям.

Жалоба «нагрелся» не указывает на виновного, а гадать дорого: в голосовом круге
непрерывно работают четыре вещи сразу, и любая из них выглядит подозрительной.
Здесь они считаются по отдельности, прямо в живом запуске.

**Меряется процессорное время потока, а не время по часам.** Нагрев — это
именно процессор: звено, которое полсекунды ждёт сеть, ноутбук не греет, а
звено, которое полсекунды считает, греет. `time.thread_time()` даёт время
**своего** потока, поэтому петлевой захват, живущий отдельным потоком, не
смешивается с голосовым кругом.

**Учёт обязан быть дешевле измеряемого.** Два обращения к часам на вызов, при
трёх звеньях и тридцати трёх кадрах в секунду это сотня обращений в секунду —
на фоне пяти процентов ядра, которые ест одна только активация, незаметно.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

logger = logging.getLogger(__name__)

#: Как часто писать сводку в лог, секунд. Ноль — не писать.
REPORT_EVERY = 60.0

#: Звенья, которые имеет смысл называть вслух в понятном порядке.
ORDER = ("имя", "речь", "эхо", "петля", "распознавание", "синтез", "разбор")


@dataclass(frozen=True, slots=True, kw_only=True)
class Load:
    """Сколько потрачено за отрезок."""

    #: Сколько прошло по часам.
    wall: float
    #: Процессорное время всего процесса.
    total: float
    #: Процессорное время по звеньям.
    stages: dict[str, float]

    @property
    def share(self) -> float:
        """Доля одного ядра, занятая процессом целиком."""
        return self.total / self.wall if self.wall > 0 else 0.0

    def shares(self) -> list[tuple[str, float]]:
        """Звенья и их доли ядра, от жадного к скромному."""
        if self.wall <= 0:
            return []
        counted = [
            (name, seconds / self.wall)
            for name, seconds in self.stages.items()
            if seconds > 0
        ]
        return sorted(counted, key=lambda item: -item[1])

    def describe(self) -> str:
        """Строка для лога: сколько всего и на что.

        Сумма звеньев меньше общего расхода, и это не ошибка учёта: в остаток
        попадает всё, что звеньями не размечено, — цикл событий, разбор JSON,
        сам Python. Видеть этот остаток полезнее, чем подгонять его к нулю.
        """
        parts = ", ".join(f"{name} {value * 100:.1f}%" for name, value in self.shares())
        counted = sum(self.stages.values())
        rest = max(0.0, self.total - counted) / self.wall if self.wall > 0 else 0.0
        tail = f", прочее {rest * 100:.1f}%" if rest > 0.001 else ""
        return f"всего {self.share * 100:.1f}% ядра ({parts}{tail})"


class Meter:
    """Счётчик процессорного времени по звеньям.

    Выключенный счётчик ничего не считает и не стоит ничего: проверка флага
    дешевле обращения к часам, а включают его не всегда.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self._stages: dict[str, float] = {}
        self._lock = threading.Lock()
        self._since = time.perf_counter()
        self._cpu_since = time.process_time()

    @property
    def enabled(self) -> bool:
        """Считаем ли вообще."""
        return self._enabled

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Засечь процессорное время куска работы.

        Замер идёт по своему потоку, поэтому чужая работа в чужих потоках сюда
        не примешивается — а именно так живёт петлевой захват.
        """
        if not self._enabled:
            yield
            return
        started = time.thread_time()
        try:
            yield
        finally:
            spent = time.thread_time() - started
            with self._lock:
                self._stages[name] = self._stages.get(name, 0.0) + spent

    def add(self, name: str, seconds: float) -> None:
        """Учесть время, замеренное снаружи."""
        if not self._enabled or seconds <= 0:
            return
        with self._lock:
            self._stages[name] = self._stages.get(name, 0.0) + seconds

    def take(self) -> Load:
        """Снять показания и начать отрезок заново."""
        now = time.perf_counter()
        cpu = time.process_time()
        with self._lock:
            stages = dict(self._stages)
            self._stages.clear()
        load = Load(
            wall=max(1e-9, now - self._since),
            total=max(0.0, cpu - self._cpu_since),
            stages=stages,
        )
        self._since = now
        self._cpu_since = cpu
        return load

    def peek(self) -> Load:
        """Посмотреть показания, не сбрасывая отрезок."""
        with self._lock:
            stages = dict(self._stages)
        return Load(
            wall=max(1e-9, time.perf_counter() - self._since),
            total=max(0.0, time.process_time() - self._cpu_since),
            stages=stages,
        )


class LoadReporter:
    """Пишет сводку в лог раз в столько-то секунд.

    Отдельным сервисом, а не задачей внутри конвейера: греет не только он, и
    привязывать общий счёт к одному из звеньев значило бы потерять остальные,
    когда конвейер не поднят.
    """

    def __init__(self, meter: Meter, *, every: float = REPORT_EVERY) -> None:
        self._meter = meter
        self._every = every
        self._task: object | None = None
        self._stop = False

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "meter"

    async def start(self) -> None:
        """Начать писать сводки."""
        import asyncio

        if self._task is not None or not self._meter.enabled or self._every <= 0:
            return
        self._stop = False
        self._task = asyncio.create_task(self._loop(), name="meter-report")

    async def stop(self) -> None:
        """Перестать писать и отчитаться напоследок."""
        self._stop = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()  # type: ignore[attr-defined]
        if self._meter.enabled:
            logger.info("Нагрузка за сеанс: %s", self._meter.peek().describe())

    async def _loop(self) -> None:
        """Раз в отрезок снимать показания и писать их."""
        import asyncio

        try:
            while not self._stop:
                await asyncio.sleep(self._every)
                logger.info("Нагрузка: %s", self._meter.take().describe())
        except asyncio.CancelledError:
            raise
