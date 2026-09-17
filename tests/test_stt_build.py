"""Сборка распознавания: основной движок и запасной."""

from __future__ import annotations

import pytest

from jarvis.core.config import STTConfig
from jarvis.core.runtime import BlockingWorker
from jarvis.core.stt import FallbackSTT, build_stt


def test_backup_whisper_gets_its_own_model_not_the_cloud_one() -> None:
    """Живой случай 14.09.2026, 18:10: сеть пропала, Whisper грузил «nova-3» и падал."""
    pytest.importorskip("faster_whisper")
    config = STTConfig(engine="deepgram", fallback="faster-whisper", api_key="k", model="nova-3", fallback_model="small")

    stt = build_stt(config, BlockingWorker())

    assert isinstance(stt, FallbackSTT)
    assert stt._backup._config.model == "small"
    assert stt._primary._config.model == "nova-3"


async def test_outage_is_reported_once_per_break() -> None:
    """Живой случай 16.09.2026: сеть пропала, а ассистент молча ждал Whisper 40 секунд."""
    from jarvis.core.errors import STTError
    from jarvis.core.stt import Transcript

    class Cloud:
        fail = True
        ready = True

        async def start(self) -> None: ...

        async def stop(self) -> None: ...

        async def transcribe(self, audio: bytes, *, sample_rate: int = 16000) -> Transcript:
            if self.fail:
                raise STTError("Сеть недоступна")
            return Transcript(text="облако")

    class Local(Cloud):
        fail = False

    cloud = Cloud()
    stt = FallbackSTT(cloud, Local(), retry_after_s=0.0)  # type: ignore[arg-type]
    warned: list[int] = []
    stt.on_outage = lambda: warned.append(1)
    await stt.transcribe(b"")
    await stt.transcribe(b"")
    assert warned == [1], "один обрыв — одно предупреждение"
    cloud.fail = False
    await stt.transcribe(b"")
    cloud.fail = True
    await stt.transcribe(b"")
    assert warned == [1, 1], "новый обрыв — снова"
