"""Запись услышанных фраз: файл на фразу, пометка имени, уборка старых дней."""

from __future__ import annotations

import wave
from datetime import datetime
from pathlib import Path

from jarvis.core.audio.recorder import UtteranceRecorder


def test_phrase_is_saved_with_name_mark_and_old_days_go(tmp_path: Path) -> None:
    recorder = UtteranceRecorder(tmp_path / "recordings", sample_rate=16000, keep_days=3)
    moment = datetime(2026, 9, 19, 12, 30, 5, 250000).timestamp()
    path = recorder.save(b"\x01\x00" * 1600, spoken_at=moment, named=True)
    assert path.parent.name == "2026-09-19" and path.name == "12-30-05-250_имя.wav"
    with wave.open(str(path)) as file:
        assert file.getframerate() == 16000 and file.getnframes() == 1600

    (tmp_path / "recordings" / "2026-09-10").mkdir()
    (tmp_path / "recordings" / "не дата").mkdir()
    assert recorder.cleanup(today=datetime(2026, 9, 19)) == 1
    assert (tmp_path / "recordings" / "2026-09-19").exists()
    assert (tmp_path / "recordings" / "не дата").exists()
