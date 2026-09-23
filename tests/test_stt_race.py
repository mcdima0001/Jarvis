"""Два распознавателя наперегонки: облако и местная модель.

Замысел владельца 23.09.2026: «пусть в два потока слушает — если Deepgram
ответил, опираемся на него, а если Whisper первее, слышим его». Жалоба была
конкретная: без интернета ассистент «очень долго думает», потому что сперва
целиком выжидался отказ облака и только потом начиналась местная работа.
"""

from __future__ import annotations

import asyncio

from jarvis.core.errors import STTError
from jarvis.core.stt import FallbackSTT
from jarvis.core.stt.protocol import Transcript


class Engine:
    """Распознаватель, который отвечает через заданное время."""

    def __init__(self, text: str, *, after: float = 0.0, fails: bool = False) -> None:
        self._text, self._after, self._fails = text, after, fails
        self.asked = 0
        self.started = 0

    @property
    def ready(self) -> bool:
        return True

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        pass

    async def transcribe(self, audio: bytes, *, sample_rate: int = 16000) -> Transcript:
        self.asked += 1
        if self._after:
            await asyncio.sleep(self._after)
        if self._fails:
            raise STTError(self._text)
        return Transcript(text=self._text, language="ru")


async def test_the_cloud_wins_when_it_answers_in_time() -> None:
    """Облако точнее: оно знает подсказанные слова и не жрёт процессор."""
    cloud, local = Engine("из облака"), Engine("местное")
    stt = FallbackSTT(cloud, local, race_after_s=0.2)  # type: ignore[arg-type]
    heard = await stt.transcribe(b"...")
    assert heard.text == "из облака"
    assert local.asked == 0, "при живом облаке местная модель даже не просыпается"


async def test_the_local_model_starts_when_the_cloud_is_late() -> None:
    """Ровно тот случай, ради которого всё затевалось: сети нет, ответ нужен."""
    cloud, local = Engine("из облака", after=10.0), Engine("местное", after=0.05)
    stt = FallbackSTT(cloud, local, race_after_s=0.05)  # type: ignore[arg-type]
    heard = await asyncio.wait_for(stt.transcribe(b"..."), timeout=2.0)
    assert heard.text == "местное", "кто первый, того и слышим"
    assert local.started == 1, "модель поднялась сама"


async def test_a_refusing_cloud_does_not_cost_the_whole_wait() -> None:
    """Отказ приходит сразу — ждать полторы секунды впустую незачем."""
    cloud = Engine("нет сети", fails=True)
    local = Engine("местное")
    stt = FallbackSTT(cloud, local, race_after_s=30.0)  # type: ignore[arg-type]
    heard = await asyncio.wait_for(stt.transcribe(b"..."), timeout=2.0)
    assert heard.text == "местное"


async def test_after_an_outage_the_cloud_is_left_alone() -> None:
    """Иначе каждая фраза начиналась бы с ожидания таймаута."""
    cloud = Engine("нет сети", fails=True)
    local = Engine("местное")
    stt = FallbackSTT(cloud, local, retry_after_s=60.0, race_after_s=0.05)  # type: ignore[arg-type]
    await stt.transcribe(b"...")
    await stt.transcribe(b"...")
    assert cloud.asked == 1, "во второй раз облако не спрашивали вовсе"
    assert local.asked == 2


async def test_the_outage_is_announced_once() -> None:
    """Об обрыве говорят один раз: повторять на каждой фразе — мучение."""
    cloud = Engine("нет сети", fails=True)
    stt = FallbackSTT(cloud, Engine("местное"), retry_after_s=0.0, race_after_s=0.05)  # type: ignore[arg-type]
    said = 0

    def told() -> None:
        nonlocal said
        said += 1

    stt.on_outage = told
    await stt.transcribe(b"...")
    await stt.transcribe(b"...")
    assert said == 1


async def test_a_broken_local_model_leaves_the_cloud_in_the_race() -> None:
    """Местная модель тоже падает — тогда ждём облако, сколько понадобится."""
    cloud = Engine("из облака", after=0.2)
    local = Engine("нет модели", fails=True)
    stt = FallbackSTT(cloud, local, race_after_s=0.01)  # type: ignore[arg-type]
    heard = await asyncio.wait_for(stt.transcribe(b"..."), timeout=2.0)
    assert heard.text == "из облака"


async def test_zero_means_both_at_once() -> None:
    """Так это и было описано: «пусть в два потока слушает»."""
    cloud, local = Engine("из облака", after=0.3), Engine("местное", after=0.05)
    stt = FallbackSTT(cloud, local, race_after_s=0.0)  # type: ignore[arg-type]
    heard = await asyncio.wait_for(stt.transcribe(b"..."), timeout=2.0)
    assert heard.text == "местное"
    assert cloud.asked == 1, "облако всё равно спрашивали — просто оно не успело"
