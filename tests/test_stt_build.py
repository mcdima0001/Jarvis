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
