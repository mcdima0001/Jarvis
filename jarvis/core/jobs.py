"""Долгая работа в фоне: принял, отпустил, доложил.

До этого модуля любая работа держала голосовой круг: команда выполнялась внутри
реплики, и человек ждал ровно столько, сколько она шла. Для секунд это годится —
для того и сделан заполнитель «секунду». Для минут не годится совсем: стоять с
открытым микрофоном, пока где-то пишется скилл, бессмысленно.

Здесь другое устройство. Поручение принимается, ассистент отвечает «займусь,
доложу» и отпускает человека. Когда работа кончится, он **заговорит сам** — тем
же швом, которым говорят сработавшие напоминания.

**Доклад обязателен, и в этом весь смысл.** Молча потерянное поручение хуже
невыполненного: человек уже на него понадеялся. Поэтому сообщается и удача, и
неудача, и даже падение с исключением — упавшая задача докладывает, что упала,
а не исчезает из списка.

**Задачи не переживают перезапуск**, и это осознанно. Пережить его могла бы
только запись «что собирались сделать», а не сама работа: возобновить
наполовину выполненный план нельзя, не зная, что из него уже случилось.
Обещать восстановление и не суметь — хуже, чем честно забыть.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Iterable

from jarvis.core.bus import EventBus
from jarvis.core.contracts import AnnouncementRequested

logger = logging.getLogger(__name__)

#: Сколько дел держать в работе одновременно.
#:
#: Предел не от бедности: у каждого дела свой доклад вслух, и пятеро
#: закончивших разом устроят монолог на полминуты. Три — столько, сколько
#: человек ещё способен держать в голове.
MAX_RUNNING = 3

#: Сколько завершённых помнить, чтобы ответить «чем кончилось».
KEEP_DONE = 10

#: Как назвать дело в докладе, если название вышло длинным.
TITLE_LIMIT = 80

_REPORT_OK = {
    "ru": "Готово: {title}. {report}",
    "en": "Done: {title}. {report}",
}

_REPORT_FAILED = {
    "ru": "Не получилось: {title}. {report}",
    "en": "Failed: {title}. {report}",
}

_BUSY = {
    "ru": "Сейчас и так три дела в работе, подожди.",
    "en": "Three things are already running, hold on.",
}

_NOTHING = {
    "ru": "Ничем не занят.",
    "en": "Nothing running.",
}

#: Чем помечается дело, снятое при выключении.
_CANCELLED = {"ru": "отменено при выключении", "en": "cancelled on shutdown"}


def shorten(text: str, *, limit: int = TITLE_LIMIT) -> str:
    """Укоротить название так, чтобы его можно было произнести."""
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


@dataclass(frozen=True, slots=True, kw_only=True)
class Job:
    """Одно фоновое поручение."""

    id: int
    title: str
    language: str = "ru"
    started: float = 0.0
    finished: float = 0.0
    #: ``None`` — ещё идёт.
    ok: bool | None = None
    report: str = ""

    @property
    def running(self) -> bool:
        """Идёт ли дело прямо сейчас."""
        return self.ok is None

    def elapsed(self, now: float | None = None) -> float:
        """Сколько идёт (или шло) в секундах."""
        end = self.finished or (now if now is not None else time.time())
        return max(0.0, end - self.started)


class Jobs:
    """Фоновые поручения: запуск, учёт и доклад по готовности."""

    def __init__(
        self,
        *,
        events: EventBus | None = None,
        notify: Callable[[str, str], None] | None = None,
        limit: int = MAX_RUNNING,
        keep: int = KEEP_DONE,
    ) -> None:
        self._events = events
        #: Куда отдавать доклад. По умолчанию — событие, которое произносит
        #: голосовой конвейер. Отдельной ручкой, чтобы политику «когда вообще
        #: уместно заговаривать» можно было поставить снаружи, не трогая здесь
        #: ничего.
        self._notify = notify or self._announce
        self._limit = max(1, limit)
        self._running: dict[int, asyncio.Task[Any]] = {}
        #: Работа, до которой задача ещё не дошла. Держим отдельно, потому что
        #: отменённая до первого шага задача не выполняет **ничего** — включая
        #: собственный обработчик отмены, — и закрыть корутину изнутри некому.
        self._work: dict[int, Awaitable[str]] = {}
        self._jobs: dict[int, Job] = {}
        self._done: deque[int] = deque(maxlen=max(1, keep))
        self._next = 1

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "jobs"

    async def start(self) -> None:
        """Ничего не поднимает: задачи появляются по поручению."""

    async def wait(self) -> None:
        """Дождаться всех текущих дел, не отменяя их.

        Нужно там, где важно, что доклад уже прозвучал: в тестах и при
        аккуратном завершении, когда торопиться некуда.
        """
        while self._running:
            await asyncio.gather(*list(self._running.values()), return_exceptions=True)

    async def stop(self) -> None:
        """Отменить всё незавершённое.

        Доклада по отменённым не будет: некому слушать — система выключается.

        **Уборка делается здесь, а не внутри задачи**, и это не придирка.
        Задача, отменённая до своего первого шага, не выполняет ничего вообще —
        в том числе собственный обработчик отмены. Поймано тестом: снятое дело
        навсегда оставалось в списке идущих.
        """
        for task in list(self._running.values()):
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._running.clear()

        for number, work in list(self._work.items()):
            self._close(work)
            self._work.pop(number, None)
        for number, job in list(self._jobs.items()):
            if job.running:
                self._finish(
                    number,
                    ok=False,
                    report=_CANCELLED.get(job.language, _CANCELLED["ru"]),
                    announce=False,
                )

    # --- учёт --------------------------------------------------------------

    @property
    def running(self) -> tuple[Job, ...]:
        """Что идёт прямо сейчас, в порядке запуска."""
        return tuple(job for job in self._jobs.values() if job.running)

    @property
    def recent(self) -> tuple[Job, ...]:
        """Недавно завершённые, самые свежие первыми."""
        return tuple(self._jobs[number] for number in reversed(self._done))

    def get(self, number: int) -> Job | None:
        """Дело по номеру."""
        return self._jobs.get(number)

    @property
    def busy(self) -> bool:
        """Заняты ли все места."""
        return len(self._running) >= self._limit

    # --- запуск ------------------------------------------------------------

    def submit(
        self, title: str, work: Awaitable[str], *, language: str = "ru"
    ) -> Job | None:
        """Взять поручение в работу.

        :param work: то, что надо сделать; должно вернуть текст доклада.
        :return: заведённое дело, либо ``None``, если все места заняты.

        Занятость не исключение, а обычный ответ: звать инструмент с отказом в
        виде исключения значит заставлять каждого вызывающего его ловить.
        """
        if self.busy:
            self._close(work)
            logger.info("Отказано в фоновой задаче %r: все места заняты", title)
            return None

        number = self._next
        self._next += 1
        job = Job(
            id=number,
            title=shorten(title),
            language=language,
            started=time.time(),
        )
        self._jobs[number] = job
        self._work[number] = work
        task = asyncio.ensure_future(self._carry(number))
        self._running[number] = task
        logger.info("Фоновая задача %d принята: %s", number, job.title)
        return job

    async def _carry(self, number: int) -> None:
        """Выполнить поручение и доложить, чем бы оно ни кончилось."""
        work = self._work.pop(number)
        ok = True
        report = ""
        try:
            report = await work
        except asyncio.CancelledError:
            # Выключение системы: докладывать некому и незачем. Запись закроет
            # `stop`, и только он: отменённая до первого шага задача сюда даже
            # не попадёт.
            self._running.pop(number, None)
            raise
        except Exception as exc:  # noqa: BLE001 — упавшая задача обязана доложить
            ok = False
            report = f"{type(exc).__name__}: {exc}"
            logger.exception("Фоновая задача %d упала", number)
        finally:
            self._running.pop(number, None)

        self._finish(number, ok=ok, report=report)

    @staticmethod
    def _close(work: Awaitable[str]) -> None:
        """Закрыть корутину, до которой так и не дошли.

        Иначе Python отругается на «coroutine was never awaited» — и будет прав.
        """
        close = getattr(work, "close", None)
        if callable(close):
            close()

    def _finish(
        self, number: int, *, ok: bool, report: str, announce: bool = True
    ) -> None:
        """Закрыть запись о деле и, если есть кому, доложить."""
        job = replace(
            self._jobs[number],
            finished=time.time(),
            ok=ok,
            report=report.strip(),
        )
        self._jobs[number] = job
        self._done.append(number)
        logger.info(
            "Фоновая задача %d кончилась за %.1f с: %s",
            number,
            job.elapsed(),
            "успех" if ok else "провал",
        )
        if announce:
            self._notify(self._report(job), job.language)

    # --- слова -------------------------------------------------------------

    @staticmethod
    def _report(job: Job) -> str:
        """Что произнести, когда дело кончилось."""
        template = (_REPORT_OK if job.ok else _REPORT_FAILED).get(
            job.language, (_REPORT_OK if job.ok else _REPORT_FAILED)["ru"]
        )
        return template.format(title=job.title, report=job.report).strip()

    def _announce(self, text: str, language: str) -> None:
        """Доклад по умолчанию — событием, которое произносит конвейер.

        Напрямую в синтез нельзя: микрофон на время речи глушит только
        конвейер, иначе ассистент услышит собственный доклад и попробует
        выполнить его как команду.
        """
        if self._events is None:
            return
        self._events.emit(
            AnnouncementRequested(source="jobs", text=text, language=language)
        )


def describe(jobs: Iterable[Job], language: str = "ru", *, now: float | None = None) -> str:
    """Перечислить дела вслух: «занят тем-то, уже пять минут»."""
    items = list(jobs)
    if not items:
        return _NOTHING.get(language, _NOTHING["ru"])
    moment = now if now is not None else time.time()
    parts = []
    for job in items:
        minutes = int(job.elapsed(moment) // 60)
        if language == "en":
            parts.append(f"{job.title} ({minutes} min)" if minutes else job.title)
        else:
            parts.append(f"{job.title} ({minutes} мин)" if minutes else job.title)
    return "; ".join(parts)


def busy_line(language: str = "ru") -> str:
    """Ответ, когда мест больше нет."""
    return _BUSY.get(language, _BUSY["ru"])
