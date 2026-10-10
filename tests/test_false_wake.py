"""Журнал ложных срабатываний имени: пишется только то, что разбудило зря."""

from __future__ import annotations

import json
import time
from pathlib import Path

from jarvis.core.audio.false_wake import AFTER_S, BEFORE_S, FalseWakeLog

RATE = 16000


def _entries(directory: Path) -> list[dict[str, object]]:
    journal = directory / "false.jsonl"
    if not journal.exists():
        return []
    return [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]


def test_a_real_call_is_not_written(tmp_path: Path) -> None:
    """Обращения владельца не пишутся никогда — ради этого и ждём приговора."""
    log = FalseWakeLog(tmp_path, sample_rate=RATE)
    now = time.time()
    log.heard(now, "джарвис", b"\x01\x00" * RATE)
    log.verdict(start=now - 0.5, end=now + 2, text="Джарвис, включи свет", named=True)
    log.tick(now + 60)
    assert _entries(tmp_path) == []
    assert not list(tmp_path.rglob("*.wav"))


def test_speech_without_the_name_is_written_with_its_sound(tmp_path: Path) -> None:
    log = FalseWakeLog(tmp_path, sample_rate=RATE)
    now = time.time()
    # До срабатывания было больше, чем держим: в запись идёт хвост.
    log.heard(now, "[unk] джарвис", b"\x01\x00" * RATE * 5)
    for _ in range(100):
        log.feed(b"\x02\x00" * 480)
    log.verdict(start=now - 3, end=now + 1, text="Не изменим своему долгу", named=False)

    [entry] = _entries(tmp_path)
    assert entry["reason"] == "не ко мне"
    assert entry["text"] == "Не изменим своему долгу"
    assert entry["heard"] == "[unk] джарвис"
    assert entry["fired_at_s"] == BEFORE_S
    wav = tmp_path / str(entry["audio"])
    assert wav.stat().st_size - 44 == int((BEFORE_S + AFTER_S) * RATE) * 2


def test_no_speech_after_the_name_is_written_as_silence(tmp_path: Path) -> None:
    log = FalseWakeLog(tmp_path, sample_rate=RATE, audio=False)
    now = time.time()
    log.heard(now, "джарвис", b"")
    log.tick(now + 1)
    assert _entries(tmp_path) == [], "приговор ещё может прийти"
    log.tick(now + 60)
    [entry] = _entries(tmp_path)
    assert entry["reason"] == "тишина" and entry["audio"] == ""


def test_a_phrase_from_another_moment_does_not_judge(tmp_path: Path) -> None:
    """Расшифровка чужой фразы не решает за срабатывание, которого в ней не было."""
    log = FalseWakeLog(tmp_path, sample_rate=RATE)
    now = time.time()
    log.heard(now, "джарвис", b"")
    log.verdict(start=now - 30, end=now - 25, text="давно сказанное", named=False)
    assert log.waiting


def test_old_sound_is_removed_but_the_journal_stays(tmp_path: Path) -> None:
    from datetime import datetime

    (tmp_path / "audio" / "2026-09-01").mkdir(parents=True)
    (tmp_path / "audio" / "2026-10-09").mkdir(parents=True)
    (tmp_path / "false.jsonl").write_text("{}\n", encoding="utf-8")
    log = FalseWakeLog(tmp_path, sample_rate=RATE, keep_days=14)
    assert log.cleanup(today=datetime(2026, 10, 10)) == 1
    assert (tmp_path / "audio" / "2026-10-09").is_dir()
    assert (tmp_path / "false.jsonl").exists()
