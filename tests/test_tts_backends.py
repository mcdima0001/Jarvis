"""Выбор движка и голоса под язык.

Движки разные не от хорошей жизни: у Kokoro сильные британские голоса, но нет
русского; Silero живее на русском, но тянет torch; Piper легче всех. Поэтому
и движок, и голос выбираются на каждый язык отдельно.
"""

from __future__ import annotations

from pathlib import Path

from jarvis.core.audio import NullAudioSink
from jarvis.core.config import TTSConfig
from jarvis.core.runtime import BlockingWorker
from jarvis.core.tts import CompositeTTS, NullTTS, build_tts, parse_voice
from jarvis.core.tts.backends import KokoroBackend, PiperBackend, SileroBackend, build_backend


def test_engine_prefix_parsed() -> None:
    """Запись «движок:голос» разбирается на части."""
    assert parse_voice("kokoro:bm_george") == ("kokoro", "bm_george")
    assert parse_voice("silero:eugene") == ("silero", "eugene")


def test_bare_voice_uses_default_engine() -> None:
    """Без префикса берётся движок по умолчанию — старые конфиги живут."""
    assert parse_voice("ru_RU-denis-medium") == ("piper", "ru_RU-denis-medium")
    assert parse_voice("bm_george", default_engine="kokoro") == ("kokoro", "bm_george")


def test_unknown_engine_falls_back() -> None:
    """Опечатка в имени движка не роняет запуск."""
    engine, voice = parse_voice("kokro:bm_george")
    assert engine == "piper"


def test_backend_built_by_name(tmp_path: Path) -> None:
    """По имени создаётся нужный движок со своим подкаталогом моделей."""
    assert isinstance(build_backend("kokoro", tmp_path), KokoroBackend)
    assert isinstance(build_backend("silero", tmp_path), SileroBackend)
    assert isinstance(build_backend("piper", tmp_path), PiperBackend)


def test_composite_resolves_engine_per_language(tmp_path: Path) -> None:
    """Для каждого языка выбирается свой движок и голос."""
    config = TTSConfig(
        voices={"ru": "silero:eugene", "en": "kokoro:bm_george"},
        default_language="ru",
        models_dir=tmp_path,
    )
    tts = CompositeTTS(config, BlockingWorker(1), sink=NullAudioSink())

    assert tts.resolve("ru") == ("ru", "silero", "eugene")
    assert tts.resolve("en") == ("en", "kokoro", "bm_george")
    assert tts.resolve("en-US") == ("en", "kokoro", "bm_george")
    # Языка нет — лучше ответить голосом по умолчанию, чем промолчать.
    assert tts.resolve("de") == ("ru", "silero", "eugene")


def test_missing_voices_disable_synthesis(tmp_path: Path) -> None:
    """Без голосов синтез отключается, а не падает при старте."""
    config = TTSConfig(voices={}, models_dir=tmp_path)
    tts = build_tts(config, BlockingWorker(1), sink=NullAudioSink())

    assert isinstance(tts, NullTTS)


def test_kokoro_speed_inverts_length_scale(tmp_path: Path) -> None:
    """У Piper больше — медленнее, у Kokoro наоборот; конфиг остаётся один."""
    slow = build_backend("kokoro", tmp_path, length_scale=2.0)
    fast = build_backend("kokoro", tmp_path, length_scale=0.5)

    assert slow._speed < 1.0 < fast._speed


# --- облачный движок --------------------------------------------------------


def test_edge_engine_recognised() -> None:
    """Облачный движок разбирается наравне с локальными."""
    assert parse_voice("edge:ru-RU-DmitryNeural") == ("edge", "ru-RU-DmitryNeural")


def test_edge_backend_needs_no_models_dir(tmp_path: Path) -> None:
    """У облачного движка нет моделей на диске."""
    from jarvis.core.tts.backends import EdgeBackend

    backend = build_backend("edge", tmp_path)
    assert isinstance(backend, EdgeBackend)
    assert backend.engine == "edge"


def test_edge_speed_converted_from_length_scale(tmp_path: Path) -> None:
    """Одна настройка скорости работает и для облачного движка.

    В конфиге скорость задана как length_scale (больше — медленнее),
    а сервису нужен процент отклонения.
    """
    normal = build_backend("edge", tmp_path, length_scale=1.0)
    slow = build_backend("edge", tmp_path, length_scale=1.5)
    fast = build_backend("edge", tmp_path, length_scale=0.5)

    assert normal._rate == "+0%"
    assert slow._rate.startswith("-")
    assert fast._rate.startswith("+")
