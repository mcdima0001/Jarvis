"""Местный синтез засчитывается в нагрузку звеном «синтез».

Живой запуск 14.09.2026: английская реплика Kokoro дала «всего 126.7% ядра,
прочее 124.8%» — синтез звеном не размечался, и вся его работа уходила в остаток.
"""

from __future__ import annotations

import threading
import time

import pytest

from jarvis.core.audio.null import NullAudioSink
from jarvis.core.config import TTSConfig
from jarvis.core.meter import Meter
from jarvis.core.runtime import BlockingWorker
from jarvis.core.tts.composite import CompositeTTS

RATE = 24000


def _burn(seconds: float) -> None:
    """Честно потратить процессорное время, а не проспать его."""
    started = time.process_time()
    while time.process_time() - started < seconds:
        sum(index * index for index in range(2000))


class _Busy:
    """Движок, который греет процессор, как местная модель."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def engine(self) -> str:
        return self._name

    def prepare(self, voice: str, language: str) -> None:
        pass

    def synthesize(self, text: str, voice: str, language: str) -> tuple[bytes, int]:
        _burn(0.1)
        return b"\0\0" * RATE, RATE


def test_cpu_stage_counts_work_of_other_threads() -> None:
    """Модель считает несколькими потоками — их работа тоже засчитывается."""
    meter = Meter()
    with meter.cpu_stage("синтез"):
        helper = threading.Thread(target=_burn, args=(0.1,))
        helper.start()
        helper.join()
    assert meter.take().stages["синтез"] >= 0.09


def test_cpu_stage_does_not_count_waiting() -> None:
    meter = Meter()
    with meter.cpu_stage("синтез"):
        time.sleep(0.2)
    assert meter.take().stages.get("синтез", 0.0) < 0.1


@pytest.mark.parametrize(("language", "counted"), [("en", True), ("ru", False)])
async def test_only_local_engines_count_as_synthesis(language: str, counted: bool) -> None:
    worker = BlockingWorker(1)
    await worker.start()
    meter = Meter()
    config = TTSConfig(voices={"ru": "edge:golos", "en": "kokoro:george"}, default_language="ru", cache_dir=None)
    tts = CompositeTTS(config, worker, sink=NullAudioSink(), meter=meter)
    tts._backends.update({"edge": _Busy("edge"), "kokoro": _Busy("kokoro")})
    try:
        # Текст на языке голоса: русскую фразу синтез честно отдал бы русскому голосу.
        text = "A long reply to check the load." if language == "en" else "Длинная реплика для проверки нагрузки."
        await tts.synthesize(text, language=language)
    finally:
        await worker.stop()
    spent = meter.take().stages.get("синтез", 0.0)
    assert (spent >= 0.09) is counted
