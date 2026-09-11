"""Фоновые поручения: принял, отпустил, доложил.

Главное, что проверяется, — **доклад приходит всегда**. Молча потерянное
поручение хуже невыполненного: человек уже на него понадеялся и ждать перестал.
Поэтому отдельно закрыты удача, неудача, падение с исключением и переполнение
очереди.
"""

from __future__ import annotations

import asyncio

from jarvis.core.bus import LocalEventBus
from jarvis.core.contracts import AnnouncementRequested
from jarvis.core.jobs import Job, Jobs, busy_line, describe, shorten


def _spy() -> tuple[Jobs, list[tuple[str, str]]]:
    """Учёт поручений с перехватом докладов."""
    said: list[tuple[str, str]] = []
    return Jobs(notify=lambda text, language: said.append((text, language))), said


async def _ok(value: str = "всё нашёл") -> str:
    """Работа, которая удаётся."""
    await asyncio.sleep(0)
    return value


async def _boom() -> str:
    """Работа, которая падает."""
    raise RuntimeError("диск отвалился")


# --- доклад -----------------------------------------------------------------


async def test_finished_job_reports_itself() -> None:
    """Закончив, ассистент заговаривает сам."""
    jobs, said = _spy()
    jobs.submit("разобрать логи", _ok())
    await jobs.wait()

    assert said == [("Готово: разобрать логи. всё нашёл", "ru")]


async def test_failed_job_reports_too() -> None:
    """Неудача докладывается наравне с удачей."""
    jobs, said = _spy()
    jobs.submit("собрать проект", _boom())
    await jobs.wait()

    text, _ = said[0]
    assert text.startswith("Не получилось: собрать проект.")
    assert "диск отвалился" in text


async def test_exception_does_not_swallow_the_job() -> None:
    """Упавшая задача остаётся в списке завершённых, а не исчезает.

    Иначе на вопрос «чем кончилось» ответить нечем, а человек ждёт.
    """
    jobs, _ = _spy()
    jobs.submit("собрать проект", _boom())
    await jobs.wait()

    assert not jobs.running
    assert len(jobs.recent) == 1
    assert jobs.recent[0].ok is False


async def test_report_language_follows_the_request() -> None:
    """Доклад приходит на языке поручения."""
    jobs, said = _spy()
    jobs.submit("check the logs", _ok("found it"), language="en")
    await jobs.wait()

    assert said == [("Done: check the logs. found it", "en")]


async def test_report_goes_through_the_bus_by_default() -> None:
    """Без своей ручки доклад уходит событием, которое произносит конвейер.

    Напрямую в синтез нельзя: микрофон на время речи глушит только конвейер,
    иначе ассистент услышит собственный доклад и выполнит его как команду.
    """
    events = LocalEventBus()
    heard: list[str] = []

    async def listen(event: AnnouncementRequested) -> None:
        """Обработчик шины обязан быть асинхронным: она его ждёт."""
        heard.append(event.text)

    events.subscribe(AnnouncementRequested.NAME, listen)  # type: ignore[arg-type]

    jobs = Jobs(events=events)
    jobs.submit("посчитать", _ok("сорок два"))
    await jobs.wait()
    await asyncio.sleep(0)

    assert heard == ["Готово: посчитать. сорок два"]


# --- пределы ----------------------------------------------------------------


async def test_places_are_limited() -> None:
    """Больше предела дел не берём.

    У каждого свой доклад вслух, и пятеро закончивших разом устроят монолог.
    """
    jobs, _ = _spy()
    held = asyncio.Event()

    async def waiting() -> str:
        await held.wait()
        return "ок"

    assert jobs.submit("раз", waiting()) is not None
    assert jobs.submit("два", waiting()) is not None
    assert jobs.submit("три", waiting()) is not None
    assert jobs.busy
    assert jobs.submit("четыре", waiting()) is None

    held.set()
    await jobs.wait()


async def test_refusal_is_an_answer_not_an_exception() -> None:
    """Отказ возвращается значением: ловить исключение у каждого вызывающего
    значит писать один и тот же `try` в каждом инструменте."""
    jobs = Jobs(notify=lambda text, language: None, limit=1)
    held = asyncio.Event()

    async def waiting() -> str:
        await held.wait()
        return "ок"

    jobs.submit("одно", waiting())
    assert jobs.submit("второе", waiting()) is None

    held.set()
    await jobs.wait()


async def test_stopping_cancels_without_reporting() -> None:
    """При выключении доклада нет: слушать его некому."""
    jobs, said = _spy()

    async def forever() -> str:
        await asyncio.Event().wait()
        return "никогда"

    jobs.submit("бесконечное", forever())
    await jobs.stop()

    assert said == []
    assert not jobs.running


# --- учёт -------------------------------------------------------------------


async def test_running_and_recent_are_kept_apart() -> None:
    """Идущее и законченное различаются, и по номеру дело находится."""
    jobs, _ = _spy()
    held = asyncio.Event()

    async def waiting() -> str:
        await held.wait()
        return "ок"

    first = jobs.submit("первое", _ok())
    await jobs.wait()
    second = jobs.submit("второе", waiting())

    assert first is not None and second is not None
    assert [job.id for job in jobs.running] == [second.id]
    assert [job.id for job in jobs.recent] == [first.id]
    assert jobs.get(first.id) is not None
    assert jobs.get(9999) is None

    held.set()
    await jobs.wait()


def test_long_title_is_shortened_for_speech() -> None:
    """Название произносится вслух, поэтому у него есть предел."""
    assert shorten("короткое") == "короткое"
    assert shorten("  лишние   пробелы  ") == "лишние пробелы"
    long = shorten("а" * 200)
    assert len(long) <= 80 and long.endswith("…")


def test_idle_assistant_says_so() -> None:
    """«Чем занят» при пустом списке — это ответ, а не молчание."""
    assert describe(()) == "Ничем не занят."
    assert describe((), "en") == "Nothing running."


def test_running_jobs_are_listed_with_their_age() -> None:
    """В списке видно, сколько дело уже идёт."""
    job = Job(id=1, title="разбор логов", started=0.0)
    assert describe([job], now=400.0) == "разбор логов (6 мин)"
    # Только что начатое возрастом не украшают: «ноль минут» — это шум.
    assert describe([job], now=10.0) == "разбор логов"


def test_busy_line_exists_in_both_languages() -> None:
    """Отказ тоже произносится, значит у него есть оба варианта."""
    assert busy_line("ru") != busy_line("en")
    assert busy_line("de") == busy_line("ru")
