"""Каждый движок синтеза проверяется своим пакетом, а не чужим."""

from __future__ import annotations

from jarvis.core.tts import _REQUIREMENTS
from jarvis.core.tts.backends import BACKENDS


def test_every_engine_names_its_own_package() -> None:
    """Без строки движок проверялся как piper: fish на сервере без piper молчал."""
    assert set(BACKENDS) <= set(_REQUIREMENTS)
