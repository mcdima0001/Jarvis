"""Теги манеры речи: «[sad]» доходит до голоса, который их понимает, и только до него.

Просьба владельца 20.10.2026: с тегами Fish говорит живее. Остальные движки
прочли бы тег вслух, поэтому для них он срезается — а подготовка текста к
синтезу не должна превратить тег в слово («сэд») для того, кто его понимает.
"""

from __future__ import annotations

import pytest

from jarvis.core.audio.null import NullAudioSink
from jarvis.core.config import TTSConfig
from jarvis.core.persona import Persona
from jarvis.core.runtime import BlockingWorker
from jarvis.core.tts.backends import FishBackend
from jarvis.core.tts.composite import CompositeTTS
from jarvis.core.tts.tags import split_tags, strip_tags

RATE = 24000


class _Backend:
    """Движок-счётчик: запоминает, что ему дали произнести."""

    def __init__(self, name: str, *, tags: bool = False, fails: bool = False) -> None:
        self._name = name
        self.fails = fails
        self.said: list[str] = []
        if tags:
            self.tags = True

    @property
    def engine(self) -> str:
        return self._name

    def prepare(self, voice: str, language: str) -> None:
        pass

    def synthesize(self, text: str, voice: str, language: str) -> tuple[bytes, int]:
        if self.fails:
            raise RuntimeError("сеть недоступна")
        self.said.append(text)
        return b"\0\0" * RATE, RATE


@pytest.fixture
def worker() -> BlockingWorker:
    return BlockingWorker(1)


def _tts(worker: BlockingWorker, backends: dict[str, _Backend]) -> CompositeTTS:
    config = TTSConfig(
        voices={"ru": "fish:jarvis", "en": "kokoro:george"},
        default_language="ru",
        fallback="kokoro:george",
    )
    tts = CompositeTTS(config, worker, sink=NullAudioSink())
    tts._backends.update(backends)
    return tts


def test_strip_tags_keeps_the_words() -> None:
    assert strip_tags("[amused] Разумеется, сэр. [sigh] Как всегда.") == "Разумеется, сэр. Как всегда."
    assert strip_tags("Готово [dry] .") == "Готово."


def test_footnotes_are_not_tags() -> None:
    """Сноски из Википедии — не указание голосу."""
    assert split_tags("Тверь основана в 1135 году[1].") == [("Тверь основана в 1135 году[1].", False)]
    assert strip_tags("Так считается[источник?].") == "Так считается[источник?]."


def test_split_tags_marks_each_tag() -> None:
    assert split_tags("[sad] Увы. [very quiet] Тихо.") == [
        ("[sad]", True), (" Увы. ", False), ("[very quiet]", True), (" Тихо.", False),
    ]


def test_fish_declares_tags() -> None:
    assert FishBackend(api_key="x").tags is True


async def test_tag_reaches_the_voice_that_understands_it(worker: BlockingWorker) -> None:
    """Текст нормализуется, тег — нет: «[amused]» не становится кириллицей."""
    await worker.start()
    fish = _Backend("fish", tags=True)
    tts = _tts(worker, {"fish": fish, "kokoro": _Backend("kokoro")})

    await tts.synthesize("[amused] Через 5 минут, сэр.", language="ru")

    assert fish.said == ["[amused] Через пять минут, сэр."]
    await worker.stop()


async def test_spare_voice_never_reads_tags(worker: BlockingWorker) -> None:
    """Облако отказало — запасной голос тег не произносит."""
    await worker.start()
    fish, kokoro = _Backend("fish", tags=True, fails=True), _Backend("kokoro")
    tts = _tts(worker, {"fish": fish, "kokoro": kokoro})

    await tts.synthesize("[sigh] Готово, сэр.", language="ru")

    assert kokoro.said and "sigh" not in kokoro.said[0]
    await worker.stop()


async def test_tag_alone_is_silence_for_a_voice_without_tags(worker: BlockingWorker) -> None:
    await worker.start()
    kokoro = _Backend("kokoro")
    tts = _tts(worker, {"fish": _Backend("fish", tags=True), "kokoro": kokoro})

    speech = await tts.synthesize("[laughing]", language="en")

    assert speech.empty
    assert not kokoro.said
    await worker.stop()


def test_tags_do_not_change_the_language_of_a_reply(worker: BlockingWorker) -> None:
    """Латиница тегов не перевешивает короткую русскую фразу."""
    tts = _tts(worker, {"fish": _Backend("fish", tags=True), "kokoro": _Backend("kokoro")})
    assert tts._spoken_language("[amused] [warm] Да, сэр, конечно.", "ru") == "ru"


def test_speaks_tags_follows_the_voice_of_the_language(worker: BlockingWorker) -> None:
    tts = _tts(worker, {"fish": _Backend("fish", tags=True), "kokoro": _Backend("kokoro")})
    assert tts.speaks_tags("ru") is True
    assert tts.speaks_tags("en") is False


def test_persona_asks_for_tags_only_when_told() -> None:
    persona = Persona()
    assert "[" not in persona.style("ru")
    assert "[sigh]" in persona.style("ru", tags=True)
    assert "[sigh]" in persona.style("en", tags=True)


class _LLM:
    """Модель, которая запоминает системную подсказку."""

    available = True

    def __init__(self) -> None:
        self.systems: list[str] = []

    async def ask(self, text: str, *, system: str = "", **_: object) -> str:
        self.systems.append(system)
        return "[amused] Разумеется, сэр."


class _Context:
    async def build(self, **_: object) -> str:
        return ""


class _Memory:
    context = _Context()

    async def remember(self, *_: object, **__: object) -> None:
        pass


@pytest.mark.parametrize("voice_tags, asked", [(lambda code: code == "ru", True), (None, False)])
async def test_chat_asks_for_tags_only_when_the_voice_speaks_them(voice_tags, asked) -> None:
    from jarvis.core.builtin import CoreTools
    from jarvis.core.tools import ToolRegistry

    llm = _LLM()
    tools = CoreTools(
        llm=llm,  # type: ignore[arg-type]
        memory=_Memory(),  # type: ignore[arg-type]
        registry=ToolRegistry(),
        skills=None,  # type: ignore[arg-type]
        voice_tags=voice_tags,
    )
    await tools.chat("как дела", language="ru")
    await tools.chat("how are you", language="en")

    assert ("[sigh]" in llm.systems[0]) is asked
    assert "[sigh]" not in llm.systems[1], "английский голос тегов не понимает"
