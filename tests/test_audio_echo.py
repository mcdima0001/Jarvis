"""Сборка эхоподавления: очередь опорного сигнала и обёртка над микрофоном.

Звуковых устройств тут нет и не нужно: микрофон подделывается списком кадров,
опорный сигнал кладётся руками. Проверяется ровно то, что нельзя проверить в
`aec.py`, — стыковка двух независимых потоков и то, что снаружи обёртка
остаётся обычным источником звука.
"""

from __future__ import annotations

from typing import AsyncIterator

import numpy
import pytest

from jarvis.core.audio.aec import BLOCK, to_float, to_pcm
from jarvis.core.audio.echo import _KEEP_MS, _MIC_DELAY_MS, EchoCancellingSource, ReferenceTrack
from jarvis.core.audio.loopback import LoopbackSource
from jarvis.core.audio.protocol import AudioFrame

RATE = 16000
FRAME = 480  # 30 мс, как в конфиге по умолчанию
#: На столько выход отстаёт от входа: линия задержки микрофона плюс блок
#: спектральной ступени. Сравнивать выход со входом без этой поправки нельзя.
DELAY = int(RATE * _MIC_DELAY_MS / 1000)


class FakeMicrophone:
    """Микрофон, отдающий заранее заготовленную волну кадрами.

    Перед каждым кадром зовётся `before` — так подкладывается опорный сигнал.
    Порядок тут не формальность: вживую петлевой захват идёт своим потоком и
    успевает раньше, а если подкладывать опору **после** кадра, она окажется
    позади микрофона, и вычитать станет нечего. Стенд должен повторять живую
    расстановку, иначе проверяет он не то.
    """

    def __init__(self, wave: numpy.ndarray, before=None) -> None:
        self._wave = wave
        self._before = before
        self.started = 0
        self.stopped = 0

    @property
    def service_name(self) -> str:
        return "audio-in"

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def frames(self) -> AsyncIterator[AudioFrame]:
        for number, at in enumerate(range(0, len(self._wave) - FRAME, FRAME)):
            if self._before is not None:
                self._before(number)
            yield AudioFrame(
                data=to_pcm(self._wave[at : at + FRAME]), sample_rate=RATE, timestamp=1.0
            )


class FakeCapture:
    """Захват опорного сигнала: считает, что его подняли и погасили."""

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start(self) -> bool:
        self.started += 1
        return True

    def stop(self) -> None:
        self.stopped += 1


async def _drain(source: EchoCancellingSource) -> numpy.ndarray:
    """Прогнать весь микрофон и собрать, что вышло."""
    out = [to_float(frame.data) async for frame in source.frames()]
    return numpy.concatenate(out) if out else numpy.zeros(0)


def _feeder(source: EchoCancellingSource, reference: numpy.ndarray):
    """Подкладывать опорный сигнал того же момента, что и очередной кадр."""

    def feed(number: int) -> None:
        source.push_reference(reference[number * FRAME : (number + 1) * FRAME])

    return feed


def _noise(samples: int, seed: int = 3) -> numpy.ndarray:
    return numpy.random.default_rng(seed).normal(0, 0.1, samples)


def test_queue_gives_back_exactly_what_was_asked() -> None:
    """Сколько сэмплов попросили, столько и приходит — куски роли не играют."""
    track = ReferenceTrack(sample_rate=RATE)
    track.push(numpy.arange(100.0))
    track.push(numpy.arange(100.0, 300.0))

    first = track.take(150)
    second = track.take(150)

    assert numpy.array_equal(first, numpy.arange(150.0))
    assert numpy.array_equal(second, numpy.arange(150.0, 300.0))
    assert track.waiting == 0


def test_queue_answers_silence_when_nothing_plays() -> None:
    """Опорного сигнала нет — это тишина, а не ошибка и не пустой массив."""
    track = ReferenceTrack(sample_rate=RATE)

    assert numpy.array_equal(track.take(BLOCK), numpy.zeros(BLOCK))


def test_queue_throws_away_stale_reference() -> None:
    """Копить опорный сигнал нельзя: старая опора хуже, чем никакой.

    Фильтр ищет эхо среди **уже полученных** кусков опоры. Если очередь
    накопила секунду, то и подаётся в него звук секундной давности — то есть
    эхо приходится искать раньше, чем оно прозвучало. Такого фильтр не умеет
    вовсе, и получается тихий отказ: всё работает, ничего не вычитается.
    """
    track = ReferenceTrack(sample_rate=RATE, max_lag_s=0.1)

    for _ in range(10):
        track.push(numpy.ones(RATE // 10))

    assert track.waiting <= RATE // 10
    assert track.dropped > 0


def test_queue_delays_the_reference_on_request() -> None:
    """Сдвиг вперёд вставляет тишину, назад — выбрасывает сэмплы."""
    track = ReferenceTrack(sample_rate=RATE)
    track.push(numpy.arange(10.0))

    track.hold(3)
    assert numpy.array_equal(track.take(5), numpy.array([0.0, 0.0, 0.0, 0.0, 1.0]))

    track.hold(-2)
    assert numpy.array_equal(track.take(2), numpy.array([4.0, 5.0]))


@pytest.mark.asyncio
async def test_microphone_passes_through_when_nothing_plays() -> None:
    """Музыки нет — запись обязана дойти до конвейера как была.

    Это главная проверка безопасности: фильтр стоит на пути **каждой** команды,
    в том числе сказанной в полной тишине.
    """
    wave = _noise(RATE)
    source = EchoCancellingSource(FakeMicrophone(wave), sample_rate=RATE, high_pass_hz=0.0)

    out = await _drain(source)

    assert len(out) > RATE // 2
    assert numpy.allclose(out[BLOCK:], wave[: len(out) - BLOCK], atol=2e-4)


@pytest.mark.asyncio
async def test_the_delay_line_holds_samples_back_instead_of_padding() -> None:
    """Придерживание микрофона обязано **удерживать** сэмплы, а не сыпать нули.

    Первая версия вставляла тишину в начало буфера, а тот вычерпывался до конца
    в том же цикле: в поток подмешивалась тишина, но звук не задерживался ни на
    сэмпл. На живом запуске это дало бесконечный рост задержки — правка
    применялась, замер не менялся, за минуту упёрлись в предел, и AEC работал в
    минус. Проверка «выход совпадает со входом» такое не ловит: при единственной
    вставке в самом начале оба поведения неотличимы.

    Ловится это счётом: у настоящей линии задержки выход **короче** входа ровно
    на её длину.
    """
    wave = _noise(4 * RATE)
    source = EchoCancellingSource(FakeMicrophone(wave), sample_rate=RATE, high_pass_hz=0.0)

    out = await _drain(source)

    held = len(wave) - len(out)
    assert abs(held - DELAY) < 2 * FRAME, f"удержано {held} сэмплов вместо {DELAY}"


@pytest.mark.asyncio
async def test_music_is_subtracted_from_the_microphone() -> None:
    """С опорным сигналом эхо из записи уходит."""
    samples = 20 * RATE
    music = _noise(samples, seed=5)
    echo = numpy.convolve(music, numpy.array([0.0] * 160 + [0.6, 0.3, -0.2, 0.1]))[:samples]
    source = EchoCancellingSource(
        FakeMicrophone(echo), sample_rate=RATE, residual=False, high_pass_hz=0.0
    )
    source._source._before = _feeder(source, music)

    out = await _drain(source)

    # Первые секунды фильтр только подбирает тракт — смотрим, к чему он пришёл.
    was = float(numpy.mean(echo[10 * RATE : 18 * RATE] ** 2))
    left = float(numpy.mean(out[BLOCK + 10 * RATE : BLOCK + 18 * RATE] ** 2))
    assert 10.0 * numpy.log10(was / left) > 20.0


@pytest.mark.asyncio
async def test_frames_keep_their_shape_and_rate() -> None:
    """Наружу выходят такие же кадры, какие пришли: конвейер ничего не заметит."""
    source = EchoCancellingSource(FakeMicrophone(_noise(RATE)), sample_rate=RATE)

    sizes = set()
    rates = set()
    async for frame in source.frames():
        sizes.add(len(frame.data))
        rates.add(frame.sample_rate)

    assert sizes == {FRAME * 2}, "кадр обязан остаться прежнего размера в байтах"
    assert rates == {RATE}


@pytest.mark.asyncio
async def test_capture_lives_and_dies_with_the_microphone() -> None:
    """Захват опорного сигнала поднимается и гасится вместе с источником."""
    capture = FakeCapture()
    source = EchoCancellingSource(FakeMicrophone(_noise(BLOCK * 4)), sample_rate=RATE)
    source.attach(capture)

    await source.start()
    await source.stop()

    assert (capture.started, capture.stopped) == (1, 1)


@pytest.mark.asyncio
async def test_missing_capture_is_not_a_failure() -> None:
    """Опорного сигнала нет вовсе — источник обязан работать как обычно."""
    microphone = FakeMicrophone(_noise(BLOCK * 4))
    source = EchoCancellingSource(microphone, sample_rate=RATE)

    await source.start()
    await source.stop()

    assert (microphone.started, microphone.stopped) == (1, 1)


@pytest.mark.asyncio
async def test_microphone_is_held_back_when_the_reference_lags() -> None:
    """Опора отстаёт сильнее начальной задержки — микрофон придерживают сильнее.

    Ровно этот случай встретился на живой машине 01.08.2026: петлевой захват
    отдавал звук на 205 мс позже микрофона (пересчёт частоты, буферы WASAPI),
    начальной задержки в 200 мс не хватало впритык, и AEC убирал 0.8 дБ вместо
    двадцати. Двигать опору вперёд тут бесполезно — тех сэмплов ещё нет, — так
    что придерживать надо микрофон, и подобрать это можно только измерением.
    """
    samples = 30 * RATE
    late = int(RATE * 0.3)
    music = _noise(samples, seed=11)
    echo = numpy.convolve(music, numpy.array([0.0] * 160 + [0.6, 0.3, -0.2, 0.1]))[:samples]
    # Опорный поток отдаёт то, что прозвучало 300 мс назад.
    delayed = numpy.concatenate((numpy.zeros(late), music))[:samples]
    source = EchoCancellingSource(
        FakeMicrophone(echo), sample_rate=RATE, residual=False, high_pass_hz=0.0
    )
    source._source._before = _feeder(source, delayed)

    out = await _drain(source)

    grown = source._delay
    assert grown > DELAY, f"задержка микрофона осталась прежней: {grown}"
    assert grown >= late + int(RATE * _KEEP_MS / 1000) - BLOCK, "придержали недостаточно"

    was = float(numpy.mean(echo[20 * RATE : 28 * RATE] ** 2))
    left = float(numpy.mean(out[BLOCK + 20 * RATE : BLOCK + 28 * RATE] ** 2))
    assert 10.0 * numpy.log10(was / left) > 15.0, "после правки эхо обязано уйти"


def test_missing_soundcard_is_reported_not_raised() -> None:
    """Нет пакета для петлевого захвата — это отказ с причиной, а не падение.

    Проверка идёт ровно на той машине, где `soundcard` не установлен: на
    сервере. Ассистент без AEC работает хуже, а упавший из-за звуковой
    библиотеки не работает вовсе.
    """
    heard: list[numpy.ndarray] = []
    capture = LoopbackSource(sample_rate=RATE, device=None, on_audio=heard.append)

    assert capture.start() is False
    assert capture.failure, "причина обязана попасть наружу"
    capture.stop()
