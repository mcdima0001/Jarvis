"""Запись всех услышанных фраз на диск — временно, чтобы разбирать ошибки слуха.

Просьба владельца 19.09.2026: «на время можно записывать всё, что я говорю,
чтобы потом понять, в чём проблемы». Без звука ошибки активации и распознавания
приходится угадывать по расшифровке, а она как раз и врёт.

Пишется **каждая** фраза, которую услышал детектор речи, а не только поданная
в распознавание: самое интересное — как раз пропущенное имя и ложное
срабатывание. Файлы лежат локально, в `memory/` (в репозиторий не едет),
по папке на день, и сами удаляются через `keep_days`. В комнате говорят не
только владелец — поэтому по умолчанию выключено и включается осознанно.
"""

from __future__ import annotations

import logging
import shutil
import wave
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


class UtteranceRecorder:
    """Складывает фразы в WAV: `<папка>/<дата>/<время>[_имя].wav`."""

    def __init__(self, directory: Path, *, sample_rate: int, keep_days: int = 3) -> None:
        self.directory = directory
        self._rate = sample_rate
        self._keep_days = max(1, keep_days)

    def cleanup(self, *, today: datetime | None = None) -> int:
        """Удалить дни старше `keep_days`. Возвращает, сколько папок удалено."""
        if not self.directory.is_dir():
            return 0
        border = (today or datetime.now()).date() - timedelta(days=self._keep_days)
        removed = 0
        for folder in self.directory.iterdir():
            try:
                day = datetime.strptime(folder.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if folder.is_dir() and day < border:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        return removed

    def save(self, audio: bytes, *, spoken_at: float, named: bool) -> Path:
        """Записать фразу. Имя файла — время её начала и пометка, звучало ли имя."""
        moment = datetime.fromtimestamp(spoken_at)
        folder = self.directory / moment.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        stem = moment.strftime("%H-%M-%S-") + f"{moment.microsecond // 1000:03d}"
        path = folder / f"{stem}{'_имя' if named else ''}.wav"
        with wave.open(str(path), "wb") as file:
            file.setnchannels(1)
            file.setsampwidth(2)
            file.setframerate(self._rate)
            file.writeframes(audio)
        return path

