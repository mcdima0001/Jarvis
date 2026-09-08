"""Кеш синтезированных реплик: одно и то же не синтезируется дважды.

Ассистент повторяется по многу раз за вечер, и не от бедности: у персоны
семьдесят с лишним служебных фраз («Слушаю внимательно, сэр», «Готово»), у
команд — свои («Пауза», «Открываю AyuGram»). Каждая такая реплика синтезируется
заново, хотя звучит ровно так же, как в прошлый раз.

Цена этого зависит от движка, но она есть всегда: у облачного Fish — около
секунды и сетевой круг на каждое «Готово», у местного Vosk — работа процессора
там, где его и так не хватает. Кеш убирает и то, и другое: файл читается за
миллисекунды.

**Что кешируется.** Только короткие реплики. Ответ поиска или свободный разговор
не повторяются никогда — они попали бы в кеш ровно один раз и остались бы в нём
мусором. Порог грубый, по длине, и это честнее умных правил: короткое почти
всегда служебное, длинное почти всегда уникальное.

**Ключ включает голос и движок.** Один и тот же текст, сказанный Kokoro и Fish,
— разный звук, и подменять один другим нельзя. При смене голоса в конфиге старые
записи просто перестают находиться и со временем вытесняются.

**Кеш переживает перезапуск** — в этом половина смысла: реплики запуска
(«Доброе утро, сэр») звучат как раз тогда, когда ничего ещё не прогрето.
"""

from __future__ import annotations

import hashlib
import io
import logging
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

#: Длиннее этого не кешируем: это уже не служебная реплика, а ответ по делу,
#: и второй раз он не прозвучит никогда.
MAX_TEXT = 160

#: Сколько файлов держать. Реплика весит около сотни килобайт, так что триста
#: штук — это десятки мегабайт: незаметно на фоне моделей синтеза, которые
#: занимают сотни.
MAX_FILES = 300

#: Сколько лишних файлов выносить за раз. Чистить по одному — значит трогать
#: диск на каждой новой реплике у полного кеша.
_EVICT_CHUNK = 50


def worth_caching(text: str) -> bool:
    """Стоит ли вообще запоминать эту реплику."""
    stripped = text.strip()
    return bool(stripped) and len(stripped) <= MAX_TEXT


class SpeechCache:
    """Готовые реплики на диске, по одной в файле."""

    def __init__(self, directory: Path, *, max_files: int = MAX_FILES) -> None:
        self._dir = directory
        self._max_files = max_files

    def _path(self, text: str, engine: str, voice: str, language: str, speed: float) -> Path:
        """Файл этой реплики.

        В ключ входит всё, что влияет на звук: движок, голос, язык и скорость.
        Забыть про скорость значило бы отдавать быструю реплику вместо медленной
        после правки конфига — и не понять, почему настройка «не применилась».
        """
        mark = f"{engine}|{voice}|{language}|{speed:.3f}|{text.strip()}"
        digest = hashlib.sha256(mark.encode("utf-8")).hexdigest()[:32]
        return self._dir / f"{digest}.wav"

    def get(self, text: str, engine: str, voice: str, language: str, speed: float) -> tuple[bytes, int] | None:
        """Достать готовую реплику. ``None`` — такой ещё не говорили."""
        path = self._path(text, engine, voice, language, speed)
        if not path.is_file():
            return None
        try:
            with wave.open(str(path)) as handle:
                audio = handle.readframes(handle.getnframes())
                rate = handle.getframerate()
        except (OSError, wave.Error) as exc:
            # Файл побился — не беда: синтезируем заново и перезапишем.
            logger.debug("Кеш реплики %s непригоден (%s)", path.name, exc)
            path.unlink(missing_ok=True)
            return None

        # Отметка времени — по ней решается, что вытеснить: реплика, которую
        # давно не говорили, скорее всего уникальная и больше не понадобится.
        try:
            path.touch()
        except OSError:  # pragma: no cover — сеть, права, гонка с уборкой
            pass
        return audio, rate

    def put(
        self,
        text: str,
        engine: str,
        voice: str,
        language: str,
        speed: float,
        audio: bytes,
        rate: int,
    ) -> None:
        """Запомнить синтезированную реплику.

        Ошибки записи гасятся: кеш — ускорение, а не работа. Не записалось —
        реплика всё равно прозвучит, просто в следующий раз её синтезируют
        снова.
        """
        if not audio:
            return
        path = self._path(text, engine, voice, language, speed)
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            # Пишем через временный файл: оборванная запись иначе оставит
            # обрубок, который потом прочитается как исправная реплика.
            temporary = path.with_suffix(".part")
            with wave.open(str(temporary), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(rate)
                handle.writeframes(audio)
            temporary.replace(path)
        except (OSError, wave.Error) as exc:
            logger.debug("Не смог сохранить реплику в кеш (%s)", exc)
            return
        self.trim()

    def trim(self) -> int:
        """Вынести самые давние реплики, если их стало слишком много.

        :return: сколько файлов убрали.
        """
        try:
            files = sorted(self._dir.glob("*.wav"), key=lambda item: item.stat().st_mtime)
        except OSError:  # pragma: no cover — каталог могли унести под ногами
            return 0
        if len(files) <= self._max_files:
            return 0

        extra = len(files) - self._max_files + _EVICT_CHUNK
        removed = 0
        for path in files[:extra]:
            try:
                path.unlink()
                removed += 1
            except OSError:  # pragma: no cover
                continue
        if removed:
            logger.info("Кеш реплик: вынес %d давних записей", removed)
        return removed
