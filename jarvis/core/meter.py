"""Кто именно греет ноутбук: учёт процессорного времени по звеньям.

Жалоба «нагрелся» не указывает на виновного, а гадать дорого: в голосовом круге
непрерывно работают четыре вещи сразу, и любая из них выглядит подозрительной.
Здесь они считаются по отдельности, прямо в живом запуске.

**Звено меряется настоящими часами, а не процессорным временем потока**
(исправлено 12.09.2026). Изначально стояло `time.thread_time()` — по смыслу
верное: нагрев это процессор, и звено, ждущее сеть, ноутбук не греет. Беда в
том, что **на Windows у этих часов шаг 15.625 мс** (замерено), а звено работает
меньше миллисекунды на кадр. Кусок в 2 мс измеряется нулём в 35 случаях из 40.

Замер двумя часами на живом запуске показал, во сколько это обходится:

| звено | `thread_time` | `perf_counter` |
|---|---|---|
| имя (vosk) | 1.28% ядра | **3.12%** |
| речь (silero) | 0.73% ядра | **1.51%** |

То есть счёт занижался в **2.4 раза**, а недостача сваливалась в «прочее» —
и отчёт вместо ответа на вопрос «кто греет» показывал «прочее 15.9%».

Плата за настоящие часы одна: в счёт попадает время, пока поток вытеснили или
он ждал GIL. Поэтому **правило ужесточается: размечать можно только
синхронный счёт без ожидания**. Обернуть сетевой запрос теперь нельзя — раньше
это давало ноль, а теперь дало бы всю задержку сети.

**Учёт обязан быть дешевле измеряемого.** Два обращения к часам на вызов, при
трёх звеньях и тридцати трёх кадрах в секунду это сотня обращений в секунду —
на фоне процентов ядра, которые ест одна только активация, незаметно.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

logger = logging.getLogger(__name__)

#: Как часто писать сводку в лог, секунд. Ноль — не писать.
REPORT_EVERY = 60.0

#: Дольше этого одно измерение звена не бывает, если внутри честный счёт.
#: Превысило — почти наверняка обёрнуто ожидание (сеть, диск, замок), а его
#: настоящие часы засчитают как нагрузку. Предупреждаем один раз на звено:
#: молча испорченный отчёт хуже, чем строка в логе.
SUSPICIOUS_S = 0.25

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
    #: Сколько ядер в машине — чтобы доля ядра не читалась как доля процессора.
    cores: int = 1

    @property
    def share(self) -> float:
        """Доля одного ядра, занятая процессом целиком."""
        return self.total / self.wall if self.wall > 0 else 0.0

    @property
    def machine_share(self) -> float:
        """Доля всего процессора, а не одного ядра.

        Разница не косметическая, и она решает споры о нагреве: «семь процентов
        ядра» на двенадцатиядерной машине — это **полпроцента процессора**, то
        есть заведомо не то, от чего греется ноутбук. Пока в отчёте стояла одна
        доля ядра, эти два числа путались.
        """
        return self.share / max(1, self.cores)

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
        machine = (
            f", это {self.machine_share * 100:.1f}% процессора" if self.cores > 1 else ""
        )
        return f"всего {self.share * 100:.1f}% ядра ({parts}{tail}){machine}"


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
        #: Начало сеанса — отдельно от начала отрезка. Пока их не различали,
        #: строка «Нагрузка за сеанс» показывала последнюю минуту: `take`
        #: сбрасывал отсчёт, а прощальный отчёт звал `peek` после него.
        self._start = self._since
        self._cpu_start = self._cpu_since
        self._session: dict[str, float] = {}
        #: Самый тяжёлый отрезок за сеанс: средняя по вечеру прячет всплески,
        #: а греется ноутбук как раз на них.
        self._peak = 0.0
        self._cores = os.cpu_count() or 1
        #: Звенья, о которых уже предупредили: одного раза достаточно.
        self._warned: set[str] = set()
        #: Последний закрытый отрезок — установившийся расход. Нужен тем, кто
        #: спрашивает сразу после сводки, когда текущий отрезок ещё пуст.
        self._last: Load | None = None

    @property
    def enabled(self) -> bool:
        """Считаем ли вообще."""
        return self._enabled

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Засечь работу куска кода.

        **Оборачивать можно только синхронный счёт.** Часы настоящие (см.
        заголовок файла), поэтому ожидание сети или диска внутри `stage`
        засчиталось бы как нагрузка — а ждущее звено ноутбук не греет.
        """
        if not self._enabled:
            yield
            return
        started = time.perf_counter()
        try:
            yield
        finally:
            spent = time.perf_counter() - started
            with self._lock:
                self._stages[name] = self._stages.get(name, 0.0) + spent
                warn = spent > SUSPICIOUS_S and name not in self._warned
                if warn:
                    self._warned.add(name)
        if warn:
            logger.warning(
                "Звено %r считалось %.2f с за один раз — похоже, внутрь попало "
                "ожидание. Настоящие часы засчитают его как нагрузку: оборачивать "
                "можно только счёт",
                name,
                spent,
            )

    def add(self, name: str, seconds: float) -> None:
        """Учесть время, замеренное снаружи."""
        if not self._enabled or seconds <= 0:
            return
        with self._lock:
            self._stages[name] = self._stages.get(name, 0.0) + seconds

    def take(self) -> Load:
        """Снять показания отрезка и начать новый.

        Отрезок закрывается, но не теряется: он ложится в счёт сеанса, а его
        доля запоминается как возможный пик.
        """
        now = time.perf_counter()
        cpu = time.process_time()
        with self._lock:
            stages = dict(self._stages)
            self._stages.clear()
            for name, seconds in stages.items():
                self._session[name] = self._session.get(name, 0.0) + seconds
        load = Load(
            wall=max(1e-9, now - self._since),
            total=max(0.0, cpu - self._cpu_since),
            stages=stages,
            cores=self._cores,
        )
        self._peak = max(self._peak, load.share)
        self._last = load
        self._since = now
        self._cpu_since = cpu
        return load

    def peek(self) -> Load:
        """Посмотреть показания текущего отрезка, не закрывая его."""
        with self._lock:
            stages = dict(self._stages)
        return Load(
            wall=max(1e-9, time.perf_counter() - self._since),
            total=max(0.0, time.process_time() - self._cpu_since),
            stages=stages,
            cores=self._cores,
        )

    def session(self) -> Load:
        """Показания за весь сеанс, от запуска до сейчас.

        Считается от своего начала, поэтому снятые по дороге отрезки его не
        обнуляют. Незакрытый отрезок входит сюда же: прощальный отчёт иначе
        терял бы последнюю минуту работы — а это ровно та минута, после которой
        обычно и выключают.
        """
        with self._lock:
            stages = dict(self._session)
            for name, seconds in self._stages.items():
                stages[name] = stages.get(name, 0.0) + seconds
        return Load(
            wall=max(1e-9, time.perf_counter() - self._start),
            total=max(0.0, time.process_time() - self._cpu_start),
            stages=stages,
            cores=self._cores,
        )

    def recent(self, *, least: float = 10.0) -> Load:
        """Показания «прямо сейчас», а не за всё время.

        Вопрос «почему греется» задают про текущее состояние, и сеанс тут врёт:
        в него входит запуск, где грузятся модели. На живом замере 12.09.2026
        сеанс показывал 27% ядра против 7% установившихся.

        Но и текущий отрезок годится не всегда: сводка в лог снимает его раз в
        минуту, и сразу после этого мерять нечего. Тогда берётся **последняя
        закрытая минута** — она и есть установившийся расход. Сеанс остаётся
        последним запасным вариантом, на первую минуту работы.
        """
        window = self.peek()
        if window.wall >= least:
            return window
        return self._last if self._last is not None else self.session()

    @property
    def peak(self) -> float:
        """Самый тяжёлый отрезок за сеанс, долей ядра."""
        return self._peak


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
            load = self._meter.session()
            peak = self._meter.peak
            tail = f"; самая тяжёлая минута — {peak * 100:.1f}% ядра" if peak else ""
            logger.info("Нагрузка за сеанс: %s%s", load.describe(), tail)

    async def _loop(self) -> None:
        """Раз в отрезок снимать показания и писать их."""
        import asyncio

        try:
            while not self._stop:
                await asyncio.sleep(self._every)
                logger.info("Нагрузка: %s", self._meter.take().describe())
        except asyncio.CancelledError:
            raise
