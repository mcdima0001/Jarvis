"""Выбор движка и голоса под язык.

Движки разные не от хорошей жизни: у Kokoro сильные британские голоса, но нет
русского; Silero живее на русском, но тянет torch; Piper легче всех. Поэтому
и движок, и голос выбираются на каждый язык отдельно.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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


async def test_edge_works_inside_running_loop() -> None:
    """Облачный движок должен работать и когда петля уже крутится.

    Интерфейс движков синхронный, а клиент внутри асинхронный. Обычно синтез
    идёт в потоке worker'а, где своей петли нет. Но служебные команды вроде
    --try-voice дёргают движок прямо из корутины CLI — и наивный asyncio.run
    там падает с «cannot be called from a running event loop».
    """
    from jarvis.core.tts.backends import _run_blocking

    async def work() -> str:
        return "готово"

    # Тест сам выполняется внутри петли — ровно та ситуация, что упала у CLI.
    assert _run_blocking(work()) == "готово"


def test_run_blocking_without_loop() -> None:
    """Без активной петли корутина выполняется напрямую."""
    from jarvis.core.tts.backends import _run_blocking

    async def work() -> int:
        return 42

    assert _run_blocking(work()) == 42


# --- Vosk -------------------------------------------------------------------


def test_vosk_engine_recognised(tmp_path: Path) -> None:
    """Vosk разбирается и собирается как остальные движки."""
    from jarvis.core.tts.backends import VoskBackend

    assert parse_voice("vosk:male_0") == ("vosk", "male_0")
    backend = build_backend("vosk", tmp_path)
    assert isinstance(backend, VoskBackend)
    assert backend.engine == "vosk"


def test_vosk_speed_inverts_length_scale(tmp_path: Path) -> None:
    """Одна настройка скорости работает и здесь: у Vosk это темп речи."""
    slow = build_backend("vosk", tmp_path, length_scale=2.0)
    fast = build_backend("vosk", tmp_path, length_scale=0.5)

    assert slow._speech_rate < 1.0 < fast._speech_rate


def test_vosk_speaker_by_name_and_number(tmp_path: Path) -> None:
    """Диктор задаётся именем из модели или номером."""
    backend = build_backend("vosk", tmp_path)
    backend._speakers = {"male_0": 3, "female_0": 0}

    assert backend._speaker_id("male_0") == 3
    assert backend._speaker_id("4") == 4
    with pytest.raises(ValueError, match="male_9"):
        backend._speaker_id("male_9")


def test_vosk_reports_missing_model(tmp_path: Path) -> None:
    """Без скачанной модели движок объясняет, что делать."""
    backend = build_backend("vosk", tmp_path)

    with pytest.raises(FileNotFoundError, match="download-voice"):
        backend.prepare("male_0", "ru")


# --- загрузка голосов -------------------------------------------------------


async def test_all_configured_voices_loaded_at_start(tmp_path: Path) -> None:
    """Голоса всех языков греются при запуске, а не в середине разговора.

    Тяжёлый движок иначе подвешивает первую же реплику своего языка на минуты,
    и со стороны это неотличимо от поломки.
    """
    config = TTSConfig(
        voices={"ru": "piper:ru_RU-denis-medium", "en": "piper:en_US-ryan-high"},
        default_language="ru",
        models_dir=tmp_path,
    )
    tts = CompositeTTS(config, BlockingWorker(1), sink=NullAudioSink())

    prepared: list[tuple[str, str]] = []

    class FakeBackend:
        """Движок, который только запоминает, что его просили подготовить."""

        engine = "piper"

        def prepare(self, voice: str, language: str) -> None:
            prepared.append((language, voice))

        def synthesize(self, text: str, voice: str, language: str) -> tuple[bytes, int]:
            return b"", 22050

    tts._backends["piper"] = FakeBackend()
    worker = tts._worker
    await worker.start()
    try:
        await tts.start()
    finally:
        await worker.stop()

    assert prepared == [("ru", "ru_RU-denis-medium"), ("en", "en_US-ryan-high")]


async def test_broken_second_voice_does_not_block_start(tmp_path: Path) -> None:
    """Без английской модели ассистент всё равно запускается и говорит по-русски."""
    config = TTSConfig(
        voices={"ru": "piper:ru_RU-denis-medium", "en": "piper:отсутствует"},
        default_language="ru",
        models_dir=tmp_path,
    )
    tts = CompositeTTS(config, BlockingWorker(1), sink=NullAudioSink())

    class HalfBrokenBackend:
        """Русский голос грузится, английского нет."""

        engine = "piper"

        def prepare(self, voice: str, language: str) -> None:
            if language == "en":
                raise FileNotFoundError("нет модели")

        def synthesize(self, text: str, voice: str, language: str) -> tuple[bytes, int]:
            return b"", 22050

    tts._backends["piper"] = HalfBrokenBackend()
    worker = tts._worker
    await worker.start()
    try:
        await tts.start()
    finally:
        await worker.stop()

    assert tts.ready


# --- эталон для XTTS --------------------------------------------------------


def test_reference_taken_from_recording(tmp_path: Path) -> None:
    """Эталон можно снять с готовой записи, а не только с другого движка.

    Это лучший путь: клон делается с живого голоса, без потери качества на
    промежуточном синтезе.
    """
    import wave

    from jarvis.core.assets import make_reference

    source = tmp_path / "мой-голос.wav"
    with wave.open(str(source), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(22050)
        handle.writeframes(b"\x00\x01" * 22050 * 8)

    target = make_reference(str(source), tmp_path / "models")

    assert target == tmp_path / "models" / "xtts" / "мой-голос.wav"
    with wave.open(str(target)) as handle:
        assert handle.getframerate() == 22050
        assert handle.getnframes() == 22050 * 8


def test_stereo_recording_rejected(tmp_path: Path) -> None:
    """Стерео-запись отбивается с понятным объяснением, а не портит эталон."""
    import wave

    from jarvis.core.assets import make_reference
    from jarvis.core.errors import JarvisError

    source = tmp_path / "stereo.wav"
    with wave.open(str(source), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"\x00\x01" * 4410)

    with pytest.raises(JarvisError, match="моно"):
        make_reference(str(source), tmp_path / "models")


def test_reference_text_matches_voice_language() -> None:
    """Эталон читается на родном языке голоса.

    Иначе движок проговаривает чужой алфавит по буквам, и клонировать нечего.
    """
    from jarvis.core.assets import _is_russian

    assert _is_russian("vosk", "male_0")
    assert _is_russian("silero", "eugene")
    assert _is_russian("piper", "ru_RU-denis-medium")
    assert _is_russian("edge", "ru-RU-DmitryNeural")
    assert not _is_russian("kokoro", "bm_george")
    assert not _is_russian("edge", "en-GB-RyanNeural")
