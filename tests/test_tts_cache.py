"""Кеш реплик: одно и то же не синтезируется дважды."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.core.audio.null import NullAudioSink
from jarvis.core.config import TTSConfig
from jarvis.core.runtime import BlockingWorker
from jarvis.core.tts.cache import MAX_TEXT, SpeechCache, worth_caching
from jarvis.core.tts.composite import CompositeTTS

RATE = 24000
AUDIO = b"\x01\x02" * RATE


def test_short_replies_are_worth_keeping() -> None:
    """Служебная реплика повторится ещё сотню раз — её и кешируем."""
    assert worth_caching("Готово.")
    assert worth_caching("Слушаю внимательно, сэр.")


def test_long_answers_are_not_kept() -> None:
    """Ответ поиска не повторится никогда и осел бы в кеше мусором.

    Порог грубый, по длине, и это честнее умных правил: короткое почти всегда
    служебное, длинное почти всегда уникальное.
    """
    assert not worth_caching("а" * (MAX_TEXT + 1))
    assert not worth_caching("   ")


def test_reply_survives_a_restart(tmp_path: Path) -> None:
    """Сохранённая реплика читается новым объектом кеша.

    В этом половина смысла: «Доброе утро, сэр» звучит как раз тогда, когда
    ничего ещё не прогрето.
    """
    SpeechCache(tmp_path).put("Готово.", "fish", "voice", "ru", 1.0, AUDIO, RATE)

    fresh = SpeechCache(tmp_path).get("Готово.", "fish", "voice", "ru", 1.0)

    assert fresh == (AUDIO, RATE)


@pytest.mark.parametrize(
    "other",
    [
        {"engine": "kokoro"},
        {"voice": "другой"},
        {"language": "en"},
        {"speed": 1.5},
    ],
)
def test_voice_and_speed_are_part_of_the_key(tmp_path: Path, other: dict) -> None:
    """Тот же текст другим голосом — другой звук, подменять нельзя.

    Скорость в ключе по той же причине: без неё после правки `length_scale`
    из кеша приходила бы прежняя реплика, и настройка выглядела бы
    «не применившейся».
    """
    cache = SpeechCache(tmp_path)
    base = {"engine": "fish", "voice": "voice", "language": "ru", "speed": 1.0}
    cache.put("Готово.", **base, audio=AUDIO, rate=RATE)

    assert cache.get("Готово.", **{**base, **other}) is None


def test_broken_file_is_not_served(tmp_path: Path) -> None:
    """Побитый файл — повод синтезировать заново, а не отдавать обрубок."""
    cache = SpeechCache(tmp_path)
    cache.put("Готово.", "fish", "voice", "ru", 1.0, AUDIO, RATE)
    next(tmp_path.glob("*.wav")).write_bytes("не wav вовсе".encode("utf-8"))

    assert cache.get("Готово.", "fish", "voice", "ru", 1.0) is None


def test_oldest_records_are_pushed_out(tmp_path: Path) -> None:
    """Кеш не растёт бесконечно: давние реплики вытесняются.

    Уникальная реплика попадает сюда один раз и больше не звучит — по времени
    последнего обращения она и уйдёт первой.
    """
    cache = SpeechCache(tmp_path, max_files=5)

    for number in range(40):
        cache.put(f"реплика {number}", "fish", "voice", "ru", 1.0, AUDIO, RATE)

    assert len(list(tmp_path.glob("*.wav"))) <= 5


async def test_cached_reply_skips_the_engine(tmp_path: Path) -> None:
    """Готовую реплику не синтезируют заново — движок даже не спрашивают.

    И не только синтез: кеш проверяется **до** прогрева, поэтому ради «Готово.»
    не поднимется модель, которая грузится полторы минуты.
    """
    calls: list[str] = []

    class _Backend:
        engine = "kokoro"

        def prepare(self, voice: str, language: str) -> None:
            calls.append(f"prepare:{voice}")

        def synthesize(self, text: str, voice: str, language: str) -> tuple[bytes, int]:
            calls.append(f"synthesize:{text}")
            return AUDIO, RATE

    config = TTSConfig(
        voices={"ru": "kokoro:george"},
        default_language="ru",
        models_dir=tmp_path,
        cache_dir=tmp_path / "cache",
    )
    worker = BlockingWorker(1)
    await worker.start()
    tts = CompositeTTS(config, worker, sink=NullAudioSink())
    tts._backends["kokoro"] = _Backend()

    first = await tts.synthesize("Готово.", language="ru")
    calls.clear()
    second = await tts.synthesize("Готово.", language="ru")

    assert second.audio == first.audio
    assert calls == [], f"движок трогали ради готовой реплики: {calls}"
    await worker.stop()


async def test_without_a_directory_nothing_is_written(tmp_path: Path) -> None:
    """Без каталога кеша синтез работает как раньше и на диск не пишет.

    Значение по умолчанию — именно такое: датакласс сам по себе не должен
    оставлять файлов, иначе тесты начинают зависеть от того, что осталось от
    прошлого запуска.
    """
    config = TTSConfig(voices={"ru": "kokoro:george"}, default_language="ru", models_dir=tmp_path)

    assert config.cache_dir is None
    assert CompositeTTS(config, BlockingWorker(1), sink=NullAudioSink())._cache is None
