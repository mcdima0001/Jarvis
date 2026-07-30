"""Лог по дням: один файл — один день.

Готового обработчика с таким поведением в стандартной библиотеке нет.
`RotatingFileHandler` режет по размеру: границы файлов приходятся на случайные
моменты, и «покажи, что было вчера» превращается в поиск по времени внутри
файла. `TimedRotatingFileHandler` режет по времени правильно, но **текущий**
день у него всегда лежит в `jarvis.log`, а даты получают только прошлые файлы —
то есть имя файла не отвечает на вопрос «за какой это день».

Поэтому свой обработчик. Он маленький и делает ровно одно: держит открытым
файл `jarvis-2026-07-30.log`, а при смене суток переключается на новый. Имя
файла и есть ответ на вопрос, что в нём лежит; сортировка по имени совпадает с
сортировкой по времени; «лог за вторник» — это один файл, а не поиск.

Просьба владельца от 30.07.2026. До этого он три дня читал один файл, который
дописывался с самого первого запуска, и решил, что тот перезаписывается каждый
сеанс: дата создания в проводнике показывала 28 июля, а внутри лежали все дни
подряд.
"""

from __future__ import annotations

import logging
import logging.handlers
import time
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)


def dated_name(base: Path, day: date) -> Path:
    """Имя файла за конкретный день: ``jarvis.log`` → ``jarvis-2026-07-30.log``.

    Дата в формате ISO намеренно: так сортировка по имени совпадает с
    сортировкой по времени, и в проводнике файлы идут по порядку сами.
    """
    return base.with_name(f"{base.stem}-{day.isoformat()}{base.suffix}")


class DailyFileHandler(logging.handlers.BaseRotatingHandler):
    """Пишет в файл текущего дня, при смене суток открывает новый.

    :param base: образец имени, например ``logs/jarvis.log``. Сам этот файл не
        создаётся — от него берутся только каталог, основа имени и расширение.
    :param keep_days: сколько дневных файлов хранить; 0 — не удалять ничего.
        Чистка идёт по имени файла, а не по дате изменения: имя надёжнее, его
        не меняет ни копирование, ни архиватор.
    """

    def __init__(self, base: Path, *, keep_days: int = 14, encoding: str = "utf-8") -> None:
        self._base = base
        self._keep_days = max(0, keep_days)
        self._day = self._today()
        super().__init__(str(dated_name(base, self._day)), "a", encoding=encoding, delay=False)
        self._cleanup()

    @staticmethod
    def _today() -> date:
        """Сегодняшняя дата по местному времени — как её видит владелец."""
        return date.fromtimestamp(time.time())

    def shouldRollover(self, record: logging.LogRecord) -> bool:  # noqa: N802 — имя из stdlib
        """Пора ли открывать новый файл: наступил следующий день."""
        return self._today() != self._day

    def doRollover(self) -> None:  # noqa: N802 — имя из stdlib
        """Закрыть вчерашний файл и открыть сегодняшний.

        Переименований тут нет вовсе — в этом и смысл: каждый файл с рождения
        назван своим днём, и трогать уже написанное не нужно.
        """
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]
        self._day = self._today()
        self.baseFilename = str(dated_name(self._base, self._day))
        self.stream = self._open()
        self._cleanup()

    def _cleanup(self) -> None:
        """Удалить дневные файлы, которых больше, чем нужно хранить."""
        if not self._keep_days:
            return
        pattern = f"{self._base.stem}-*{self._base.suffix}"
        try:
            found = sorted(self._base.parent.glob(pattern))
        except OSError as exc:
            logger.debug("Не смог перечислить старые логи: %s", exc)
            return
        for path in found[: max(0, len(found) - self._keep_days)]:
            try:
                path.unlink()
            except OSError as exc:
                # Не смогли удалить — не беда: логи важнее уборки за ними.
                logger.debug("Не смог удалить старый лог %s: %s", path, exc)
