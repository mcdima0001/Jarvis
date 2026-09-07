"""Запасной голос: чем говорить, когда основной отказал.

С облачным синтезом это не роскошь, а условие работоспособности: без сети
ассистент иначе немеет целиком — и не может даже сказать, что случилось.
"""

from __future__ import annotations

import pytest

from jarvis.core.audio.null import NullAudioSink
from jarvis.core.config import TTSConfig
from jarvis.core.runtime import BlockingWorker
from jarvis.core.tts.composite import CompositeTTS

RATE = 24000

#: Имена движков тут настоящие, хотя сами движки подделаны. `parse_voice`
#: сверяет префикс с закрытым списком `BACKENDS` и незнакомый молча заменяет
#: движком по умолчанию — выдуманные «cloud» и «local» превращались в piper, и
#: проверка меряла не то. Настоящий облачный движок в паре с местным заодно
#: повторяет живую расстановку.


class _Backend:
    """Движок-счётчик: говорит или отказывает."""

    def __init__(self, name: str, *, fails: bool = False) -> None:
        self._name = name
        self.fails = fails
        self.said: list[tuple[str, str, str]] = []
        self.prepared: list[tuple[str, str]] = []

    @property
    def engine(self) -> str:
        return self._name

    def prepare(self, voice: str, language: str) -> None:
        self.prepared.append((voice, language))

    def synthesize(self, text: str, voice: str, language: str) -> tuple[bytes, int]:
        if self.fails:
            raise RuntimeError("сеть недоступна")
        self.said.append((text, voice, language))
        return b"\0\0" * RATE, RATE


@pytest.fixture
def worker() -> BlockingWorker:
    return BlockingWorker(1)


def _tts(worker: BlockingWorker, backends: dict[str, _Backend], **overrides) -> CompositeTTS:
    """Синтез с подставленными движками вместо настоящих."""
    settings = {
        "voices": {"ru": "edge:golos", "en": "kokoro:george"},
        "default_language": "ru",
        "fallback": "kokoro:george",
    }
    settings.update(overrides)
    config = TTSConfig(**settings)
    tts = CompositeTTS(config, worker, sink=NullAudioSink())
    tts._backends.update(backends)
    return tts


async def test_main_voice_is_used_while_it_works(worker: BlockingWorker) -> None:
    """Пока основной голос отвечает, запасной молчит."""
    await worker.start()
    cloud, local = _Backend("edge"), _Backend("kokoro")
    tts = _tts(worker, {"edge": cloud, "kokoro": local})

    speech = await tts.synthesize("Слушаю внимательно, сэр.", language="ru")

    assert speech.language == "ru"
    assert len(cloud.said) == 1
    assert not local.said
    await worker.stop()


async def test_refusal_switches_to_the_spare_voice(worker: BlockingWorker) -> None:
    """Основной отказал — говорит запасной, а не тишина."""
    await worker.start()
    cloud, local = _Backend("edge", fails=True), _Backend("kokoro")
    tts = _tts(worker, {"edge": cloud, "kokoro": local})

    speech = await tts.synthesize("Готово.", language="ru")

    assert not speech.empty, "ассистент промолчал вместо аварийного голоса"
    assert len(local.said) == 1
    await worker.stop()


async def test_russian_reaches_the_english_voice_readable(worker: BlockingWorker) -> None:
    """Русский текст уходит в английский голос латиницей, а не кириллицей.

    Кириллицу английский движок читает как кашу либо молчит. Язык при синтезе
    берётся от **голоса**, а не от вопроса, и `normalize_for_speech` переводит
    текст под него: получается с акцентом, но разборчиво — для аварийного
    режима этого достаточно.
    """
    await worker.start()
    cloud, local = _Backend("edge", fails=True), _Backend("kokoro")
    tts = _tts(worker, {"edge": cloud, "kokoro": local})

    await tts.synthesize("Слушаю, сэр.", language="ru")

    spoken, _, language = local.said[0]
    assert language == "en", "запасной голос получил чужой язык"
    assert not any("а" <= letter.lower() <= "я" for letter in spoken), (
        f"кириллица дошла до английского голоса: {spoken!r}"
    )


async def test_main_voice_is_not_retried_on_every_line(worker: BlockingWorker) -> None:
    """После отказа основной голос не трогают некоторое время.

    Реплики звучат десятки раз за вечер, и ждать таймаут облака на каждой
    значит превратить обрыв связи в «ассистент задумывается перед каждым
    словом».
    """
    await worker.start()
    cloud, local = _Backend("edge", fails=True), _Backend("kokoro")
    tts = _tts(worker, {"edge": cloud, "kokoro": local})

    for _ in range(3):
        await tts.synthesize("Готово.", language="ru")

    assert len(local.said) == 3
    assert cloud.said == [], "основной голос не должен был ответить ни разу"
    await worker.stop()


async def test_main_voice_gets_another_chance_later(worker: BlockingWorker) -> None:
    """Сеть вернулась — возвращаемся к основному голосу."""
    await worker.start()
    cloud, local = _Backend("edge", fails=True), _Backend("kokoro")
    tts = _tts(worker, {"edge": cloud, "kokoro": local})

    await tts.synthesize("Раз.", language="ru")
    cloud.fails = False
    tts._blocked_until = 0.0
    await tts.synthesize("Два.", language="ru")

    assert len(cloud.said) == 1
    await worker.stop()


async def test_without_a_spare_the_error_is_not_hidden(worker: BlockingWorker) -> None:
    """Запасного нет — ошибка идёт наверх, а не превращается в тишину.

    Молчание без объяснения — худший из исходов: со стороны оно неотличимо от
    «ассистент не расслышал».
    """
    await worker.start()
    cloud = _Backend("edge", fails=True)
    tts = _tts(worker, {"edge": cloud}, fallback="")

    with pytest.raises(RuntimeError, match="сеть недоступна"):
        await tts.synthesize("Готово.", language="ru")
    await worker.stop()


def test_spare_outside_voices_is_reported(
    worker: BlockingWorker, caplog: pytest.LogCaptureFixture
) -> None:
    """Запасной голос не из `voices` — предупреждение при сборке.

    Такой голос не загрузится при старте и в нужный момент окажется так же
    недоступен, как основной. Молчащий запасной хуже отсутствующего: о нём
    думают, что он есть.
    """
    config = TTSConfig(
        voices={"ru": "edge:golos"},
        default_language="ru",
        fallback="kokoro:никого-нет",
    )

    with caplog.at_level("WARNING", logger="jarvis.core.tts.composite"):
        CompositeTTS(config, worker, sink=NullAudioSink())

    assert "не указан ни для одного языка" in caplog.text


def test_spare_equal_to_main_is_not_a_spare(worker: BlockingWorker) -> None:
    """Запасной, совпадающий с основным, откатом не является."""
    config = TTSConfig(
        voices={"ru": "kokoro:george"},
        default_language="ru",
        fallback="kokoro:george",
    )
    tts = CompositeTTS(config, worker, sink=NullAudioSink())

    assert tts._spare("kokoro", "george") is None

async def test_start_survives_a_dead_main_voice(worker: BlockingWorker) -> None:
    """Основной голос не поднялся — запускаемся на запасном, а не падаем.

    С облачным голосом это обычное дело: нет сети — нет и голоса. Ассистент без
    синтеза работает хуже, а не запустившийся не работает вовсе, поэтому пока
    есть чем говорить, старт обязан состояться.
    """
    await worker.start()

    class _Dead(_Backend):
        def prepare(self, voice: str, language: str) -> None:
            raise RuntimeError("нет сети")

    cloud, local = _Dead("edge"), _Backend("kokoro")
    tts = _tts(worker, {"edge": cloud, "kokoro": local})

    await tts.start()
    speech = await tts.synthesize("Готово.", language="ru")

    assert not speech.empty, "ассистент онемел, хотя запасной голос был"
    assert len(local.said) == 1
    await worker.stop()


async def test_start_still_fails_without_a_spare(worker: BlockingWorker) -> None:
    """Запасного нет — молча запускаться без голоса нельзя.

    Тихий запуск без синтеза выглядит как исправный, пока ассистента не
    позовут; лучше отказать сразу и назвать причину.
    """
    await worker.start()

    class _Dead(_Backend):
        def prepare(self, voice: str, language: str) -> None:
            raise RuntimeError("нет модели")

    tts = _tts(worker, {"edge": _Dead("edge")}, fallback="")

    with pytest.raises(RuntimeError, match="нет модели"):
        await tts.start()
    await worker.stop()
