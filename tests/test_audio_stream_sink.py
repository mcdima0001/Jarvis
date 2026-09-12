"""Потоковый вывод звука: тот самый код, что играет реплику по кускам.

Звуковой карты тут нет — `sounddevice` подделан. Но проверяется **настоящий**
`SoundDeviceSink.play_stream`, а не его пересказ, и это важно: ошибка в нём
означает не кривой звук, а немого ассистента, и заметить её на живой машине
можно только ухом.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import pytest

from jarvis.core.audio import devices
from jarvis.core.audio.devices import PREBUFFER_MS, SoundDeviceSink
from jarvis.core.config import AudioConfig

RATE = 24000

#: Кусок в четверть секунды — ровно столько же, сколько придерживает буфер.
CHUNK = b"\0\0" * (RATE // 4)


class _Stream:
    """Поддельный вывод PortAudio: помнит, что в него написали."""

    def __init__(self, board: "_Board", **kwargs: Any) -> None:
        self._board = board
        board.opened.append(kwargs)

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *exc: object) -> None:
        self._board.closed += 1

    def write(self, data: bytes) -> None:
        self._board.written.append(bytes(data))


class _Board:
    """Подделка модуля sounddevice."""

    def __init__(self, *, fails: bool = False) -> None:
        self.opened: list[dict[str, Any]] = []
        self.written: list[bytes] = []
        self.closed = 0
        self._fails = fails

    def RawOutputStream(self, **kwargs: Any) -> _Stream:  # noqa: N802 — имя чужого API
        if self._fails:
            raise RuntimeError("устройство занято")
        return _Stream(self, **kwargs)


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch) -> _Board:
    """Подставить поддельную звуковую подсистему вместо настоящей."""
    fake = _Board()
    monkeypatch.setattr(devices, "_import_sounddevice", lambda: fake)
    return fake


def _sink() -> SoundDeviceSink:
    return SoundDeviceSink(AudioConfig())


async def _feed(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def test_stream_reaches_the_card_in_order(board: _Board) -> None:
    """Куски доходят до вывода целиком и в том же порядке."""
    await _sink().play_stream(_feed(CHUNK, CHUNK, CHUNK), sample_rate=RATE)

    assert b"".join(board.written) == CHUNK * 3
    assert len(board.opened) == 1, "поток должен открывать устройство один раз"
    assert board.closed == 1
    assert board.opened[0]["samplerate"] == RATE


async def test_device_opens_only_after_the_prebuffer(board: _Board) -> None:
    """Играть начинаем, накопив запас: иначе неровная сеть слышна щелчком.

    Запас платится один раз на реплику, а щелчок портит каждую, в которой
    случился, — поэтому размен в пользу запаса.
    """
    small = b"\0\0" * 10
    await _sink().play_stream(_feed(small), sample_rate=RATE)
    # Одного крошечного куска на запас не хватило, но и терять его нельзя.
    assert b"".join(board.written) == small

    board.opened.clear()
    board.written.clear()
    enough = b"\0\0" * int(RATE * PREBUFFER_MS / 1000)
    await _sink().play_stream(_feed(enough, CHUNK), sample_rate=RATE)
    assert b"".join(board.written) == enough + CHUNK


async def test_empty_stream_touches_nothing(board: _Board) -> None:
    """Пустой поток не открывает устройство и ничем не заканчивается."""
    await _sink().play_stream(_feed(), sample_rate=RATE)

    assert not board.opened
    assert not board.written


async def test_empty_chunks_are_skipped(board: _Board) -> None:
    """Пустой кусок в потоке — не конец потока и не повод открывать вывод."""
    await _sink().play_stream(_feed(b"", CHUNK, b""), sample_rate=RATE)

    assert b"".join(board.written) == CHUNK


async def test_broken_device_does_not_hang_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отказ вывода не вешает того, кто наполняет поток, и не рвёт разговор.

    Поток разбирается до конца даже при сбое: иначе синтез упёрся бы в
    неразобранные куски и остался ждать навсегда — ассистент завис бы молча.
    """
    fake = _Board(fails=True)
    monkeypatch.setattr(devices, "_import_sounddevice", lambda: fake)
    given = 0

    async def feed() -> AsyncIterator[bytes]:
        nonlocal given
        for _ in range(6):
            given += 1
            yield CHUNK

    await _sink().play_stream(feed(), sample_rate=RATE)

    assert given == 6, "поток не дочитали до конца"
    assert not fake.written


async def test_replies_do_not_overlap(board: _Board) -> None:
    """Замок тот же, что у обычного воспроизведения: две реплики не лезут разом."""
    sink = _sink()
    await sink.play_stream(_feed(CHUNK), sample_rate=RATE)
    await sink.play(CHUNK, sample_rate=RATE)

    assert len(board.opened) == 2


async def test_odd_chunks_stay_aligned_to_samples(board: _Board) -> None:
    """Кусок нечётной длины — половина отсчёта, и отдать её карте нельзя.

    Сеть режет поток где придётся. Отданная половина сдвинула бы на байт всё
    последующее: старший байт стал бы младшим, и вместо речи пошёл бы шум до
    конца реплики. Хвост придерживается до следующего куска.
    """
    audio = bytes(range(256)) * 8
    # Режем как попало — так, чтобы границы не совпадали с отсчётами.
    cuts = [audio[i : i + 333] for i in range(0, len(audio), 333)]
    assert any(len(cut) % 2 for cut in cuts), "куски вышли чётными, проверка ни о чём"

    await _sink().play_stream(_feed(*cuts), sample_rate=RATE)

    written = b"".join(board.written)
    assert all(len(part) % 2 == 0 for part in board.written), "в карту ушла половина отсчёта"
    assert written == audio[: len(written)]
    assert len(audio) - len(written) <= 1, "потеряли больше одного лишнего байта"
