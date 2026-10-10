"""Журнал ложных срабатываний имени: что услышал детектор и что было на самом деле.

Просьба владельца 10.10.2026: «часто срабатывает впустую — записывай, разберём и
добавим в слова-соперники». Лог и так говорил, **что** прозвучало (расшифровка
облака без «Джарвис»), но не говорил, **какое слово** в этой фразе детектор
принял за имя. А соперник нужен ровно это слово: в «Не изменим своему долгу.
Я знаю…» кандидатов десять. Найти его можно только по звуку — прогнать запись
через полный словарь модели и посмотреть, что звучало в момент срабатывания
(`tools/wake_false.py`).

Поэтому на каждое срабатывание держится короткая запись вокруг него — до и
после — и **ждёт приговора**:

* в расшифровке есть имя — обращались по делу, запись выбрасывается;
* расшифровка без имени — «не ко мне», запись ложится на диск;
* без имени в тексте, но ушла командой («команда без имени») — тоже на диск:
  это либо исковерканное облаком имя, либо чужая речь, принятая за команду, и
  отличить одно от другого можно только по звуку;
* речи после срабатывания не было вовсе — «тишина», тоже на диск.

Обращения с именем в расшифровке, таким образом, не пишутся никогда, а чужая
речь — только та, что разбудила ассистента. Всё лежит в `memory/` (в git не едет), звук
сам удаляется через `keep_days`, строки журнала остаются: они весят байты.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import wave
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

#: Имя файла журнала в каталоге.
JOURNAL = "false.jsonl"
#: Подкаталог звука.
AUDIO = "audio"

#: Сколько звука держать до срабатывания, с. Детектор срабатывает, когда имя
#: продержалось в гипотезе `hold_ms`, то есть уже на втором слоге: слово целиком
#: и соседнее перед ним влезают с запасом.
BEFORE_S = 2.5
#: Сколько дописать после: конец слова и следующее за ним.
AFTER_S = 1.0
#: Сколько ждать приговора. Окно ответа плюс время расшифровки: фраза после
#: имени может звучать все `follow_up_s` и распознаваться ещё пару секунд.
VERDICT_S = 20.0


@dataclass
class _Pending:
    """Срабатывание, ждущее приговора."""

    at: float
    heard: str
    audio: bytearray
    #: Сколько байт ещё дописать после срабатывания.
    missing: int
    deadline: float
    #: Где в записи срабатывание, байт от начала.
    fired: int = 0
    floor_db: float | None = None
    playing: tuple[str, ...] = field(default_factory=tuple)


class FalseWakeLog:
    """Копит звук вокруг срабатывания и пишет на диск только ложные."""

    def __init__(
        self,
        directory: Path,
        *,
        sample_rate: int,
        keep_days: int = 14,
        audio: bool = True,
        playing: Callable[[], Awaitable[Sequence[str]]] | None = None,
    ) -> None:
        self.directory = directory
        self._rate = sample_rate
        self._keep_days = max(1, keep_days)
        self._audio = audio
        self._playing = playing
        self._pending: _Pending | None = None
        #: Ссылку держим, чтобы задачу не собрал сборщик мусора на полпути.
        self._task: asyncio.Task[None] | None = None

    @property
    def before_bytes(self) -> int:
        """Сколько байт звука держать до срабатывания — по нему конвейер режет кольцо."""
        return int(BEFORE_S * self._rate) * 2

    @property
    def waiting(self) -> bool:
        """Есть ли срабатывание без приговора."""
        return self._pending is not None

    def heard(self, at: float, hypothesis: str, before: bytes, *, floor_db: float | None = None) -> None:
        """Детектор услышал имя: начать запись и ждать, по делу ли.

        Повторное срабатывание, пока первое ждёт, ничего не начинает: «Джарвис
        Джарвис» — одна фраза, и приговор у неё один.
        """
        if self._pending is not None:
            return
        kept = before[-self.before_bytes:]
        self._pending = _Pending(
            at=at,
            heard=hypothesis,
            audio=bytearray(kept),
            missing=int(AFTER_S * self._rate) * 2,
            deadline=at + VERDICT_S,
            fired=len(kept),
            floor_db=floor_db,
        )
        if self._playing is not None:
            pending = self._pending
            self._task = asyncio.get_running_loop().create_task(self._note_playing(pending))

    async def _note_playing(self, pending: _Pending) -> None:
        """Что звучало в момент срабатывания: аниме, музыка, созвон."""
        assert self._playing is not None
        try:
            pending.playing = tuple(await self._playing())
        except Exception as exc:  # noqa: BLE001 — без этого запись всё равно полезна
            logger.debug("Не узнал, что играет: %s", exc)

    def feed(self, data: bytes) -> None:
        """Очередной кадр микрофона — дописать хвост после срабатывания."""
        pending = self._pending
        if pending is None or pending.missing <= 0:
            return
        piece = data[: pending.missing]
        pending.audio.extend(piece)
        pending.missing -= len(piece)

    def verdict(
        self, *, start: float, end: float, text: str, named: bool, reason: str = "не ко мне"
    ) -> None:
        """Фраза расшифрована — решить судьбу срабатывания, если оно в ней.

        :param start: когда фраза началась (стенные часы).
        :param end: когда кончилась.
        :param named: звали ли в ней ассистента.
        """
        pending = self._pending
        if pending is None or not start - 1.0 <= pending.at <= end + 1.0:
            return
        self._pending = None
        if not named:
            self._write(pending, text=text, reason=reason)

    def tick(self, now: float) -> None:
        """Приговора так и не было — значит, после «имени» речи не было."""
        pending = self._pending
        if pending is not None and now >= pending.deadline:
            self._pending = None
            self._write(pending, text="", reason="тишина")

    def cleanup(self, *, today: datetime | None = None) -> int:
        """Удалить звук старше `keep_days`. Журнал не трогается: он весит байты."""
        folder = self.directory / AUDIO
        if not folder.is_dir():
            return 0
        border = (today or datetime.now()).date() - timedelta(days=self._keep_days)
        removed = 0
        for day in folder.iterdir():
            try:
                date = datetime.strptime(day.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day.is_dir() and date < border:
                shutil.rmtree(day, ignore_errors=True)
                removed += 1
        return removed

    def _write(self, pending: _Pending, *, text: str, reason: str) -> None:
        """Записать ложное срабатывание — в фоне, не задерживая слух."""
        moment = datetime.fromtimestamp(pending.at)
        name = ""
        if self._audio and pending.audio:
            name = f"{AUDIO}/{moment:%Y-%m-%d}/{moment:%H-%M-%S}-{moment.microsecond // 1000:03d}.wav"
        entry = {
            "at": moment.isoformat(timespec="milliseconds"),
            "heard": pending.heard,
            "text": text,
            "reason": reason,
            "playing": list(pending.playing),
            "floor_db": None if pending.floor_db is None else round(pending.floor_db, 1),
            # Где в записи срабатывание: по нему разбор ищет слово-виновника.
            "fired_at_s": round(pending.fired / 2 / self._rate, 2),
            "audio": name,
        }
        audio = bytes(pending.audio)

        def write() -> None:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                if name:
                    path = self.directory / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with wave.open(str(path), "wb") as file:
                        file.setnchannels(1)
                        file.setsampwidth(2)
                        file.setframerate(self._rate)
                        file.writeframes(audio)
                with (self.directory / JOURNAL).open("a", encoding="utf-8") as file:
                    file.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError as exc:
                logger.warning("Ложное срабатывание не записалось: %s", exc)

        logger.info(
            "Ложное срабатывание имени (%s; детектор: %r)%s",
            reason,
            pending.heard,
            f", записал {name}" if name else "",
        )
        try:
            asyncio.get_running_loop().run_in_executor(None, write)
        except RuntimeError:  # вне цикла событий — в тестах
            write()
