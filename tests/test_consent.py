"""Отказ отменяет необратимое: «нет», «стоп» и переспрос сквозь всю систему.

Аудит 01.10.2026 нашёл три дороги, по которым отвергнутое всё же выполнялось:
прерванный план продолжался по совпадению цели, а не по согласию; «стоп»
конвейер глотал как «замолчи», оставляя вопрос висеть; «Точно?» читалось как
«да». Здесь собраны настоящие конвейер, диспетчер и план — подставлены только
модель и отправка.
"""

from __future__ import annotations

import time
from typing import Any

from jarvis.core.bus import LocalEventBus
from jarvis.core.config import AudioConfig, TaskProfile, WakeWordConfig
from jarvis.core.contracts import Intent, ToolResult, Utterance
from jarvis.core.llm import LLMRequest, LLMResponse, LLMService, ProfileRegistry, ToolCall
from jarvis.core.router import Dispatcher, PhraseResolver, Router
from jarvis.core.tools import ToolRegistry, collect_tools, tool
from jarvis.core.tts import NullTTS
from jarvis.core.voice import VoicePipeline


class Phone:
    """Сообщение маме: прямой вопрос «отправить?» и сама отправка."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    @tool(phrases=["напиши маме"], reversible=True)
    async def compose(self) -> ToolResult:
        """Собрать сообщение и спросить разрешения."""
        return ToolResult.asking(
            Intent(tool="phone.send", arguments={"text": "буду через час"}),
            question="Отправить маме «буду через час»?",
        )

    @tool(reversible=False)
    async def send(self, text: str = "") -> ToolResult:
        """Отправить сообщение маме."""
        self.sent.append(text)
        return ToolResult.success({"sent": text}, speech="Отправил.")

    @tool(phrases=["включи свет"], reversible=True)
    async def light(self) -> ToolResult:
        """Включить свет."""
        return ToolResult.success(True, speech="Свет включён.")


class Scripted:
    """Модель плана: на каждый ход — отправить сообщение маме."""

    name = "scripted"
    configured = True

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            tool_calls=(ToolCall(name="phone__send", arguments={"text": "неверный текст"}),), usage={}
        )

    async def aclose(self) -> None:
        """Закрывать нечего."""


class ToPlan:
    """Всё, что не узнали фразы, — в план целиком, как делает модель разбора."""

    name = "llm"

    async def resolve(self, utterance: Utterance) -> Intent | None:
        return Intent(tool="core.plan", arguments={"goal": utterance.cleaned}, confidence=0.9)


class Said(NullTTS):
    """Синтез, который помнит сказанное."""

    def __init__(self) -> None:
        super().__init__()
        self.said: list[str] = []

    async def say(self, text: str, *, language: str | None = None) -> None:
        self.said.append(text)


def _system(*, plan: bool = False) -> tuple[Phone, Dispatcher, VoicePipeline, Said]:
    """Конвейер, диспетчер и, по желанию, настоящий план поверх подставной модели."""
    from jarvis.core.audio import NullAudioSink, NullAudioSource, PassthroughVAD
    from jarvis.core.audio.null import AlwaysActiveWakeWord
    from jarvis.core.builtin import CoreTools
    from jarvis.core.stt import NullSTT

    events = LocalEventBus()
    registry = ToolRegistry(events=events, default_timeout=1.0)
    phone = Phone()
    for item in collect_tools(phone, namespace="phone"):
        registry.register(item)
    resolvers: list[Any] = [PhraseResolver(registry)] + ([ToPlan()] if plan else [])
    dispatcher = Dispatcher(router=Router(resolvers, threshold=0.6), registry=registry, events=events)
    if plan:
        llm = LLMService(
            providers={"scripted": Scripted()},  # type: ignore[dict-item]
            profiles=ProfileRegistry(
                {"plan": TaskProfile(task="plan", provider="scripted", model="stub")},
                default_task="plan",
            ),
        )
        core = CoreTools(llm=llm, memory=None, registry=registry, skills=None)  # type: ignore[arg-type]
        for item in collect_tools(core, namespace="core"):
            registry.register(item)
        # Так же, как в `app.build`: снятый вопрос забирает у плана разрешённый шаг.
        dispatcher.on_drop(core.withdraw)
    tts = Said()
    pipeline = VoicePipeline(
        source=NullAudioSource(),
        sink=NullAudioSink(),
        vad=PassthroughVAD(),
        wake_word=AlwaysActiveWakeWord("джарвис"),
        stt=NullSTT(),
        tts=tts,
        dispatcher=dispatcher,
        events=events,
        config=AudioConfig(
            working_after_s=0.0,
            wake_word=WakeWordConfig(mode="text", phrases=("джарвис",), follow_up_s=10.0),
        ),
    )
    return phone, dispatcher, pipeline, tts


def _text(text: str, source: str = "panel") -> Utterance:
    return Utterance(text=text, language="ru", source=source)


async def _ask(pipeline: VoicePipeline, text: str = "напиши маме") -> ToolResult:
    asked = await pipeline.handle(_text(text, source="voice"))
    assert asked.confirm is not None, "вопрос не задан"
    return asked


# --- «стоп» в ответ на вопрос — это «нет» ------------------------------------


async def test_stop_then_yes_sends_nothing() -> None:
    """«Отправить маме?» — «Стоп» — «да»: не отправлено (аудит 01.10.2026).

    «Стоп» перехватывался как «замолчи» мимо диспетчера, вопрос висел ещё
    полторы минуты с открытым окном ответа без имени, и любое «да» в комнате
    отправляло то, от чего владелец только что отказался.
    """
    from jarvis.core.router.dispatcher import _DROPPED

    for word in ("Стоп!", "хватит", "stop", "enough", "замолчи"):
        phone, dispatcher, pipeline, tts = _system()
        await _ask(pipeline)

        await pipeline.handle(_text(word))

        assert dispatcher.awaiting is None, word
        assert pipeline._follow_up_until == 0.0, f"{word}: окно ответа без имени осталось открытым"
        assert tts.said[-1] in _DROPPED["ru"], f"{word}: отказ не подтверждён вслух"
        await pipeline.handle(_text("да"))
        assert phone.sent == [], word


async def test_stop_without_a_question_only_hushes_and_closes_the_window() -> None:
    """Без вопроса «стоп» — просто «замолчи»: ни реплики, ни открытого окна."""
    _, _, pipeline, tts = _system()
    pipeline._follow_up_until = time.time() + 10

    hushed = await pipeline.handle(_text("стоп"))

    assert hushed.value == {"hushed": True}
    assert pipeline._follow_up_until == 0.0
    assert tts.said == []


# --- переспрос — не согласие --------------------------------------------------


async def test_asking_back_is_not_consent() -> None:
    """«Отправить маме?» — «Точно?»: сообщение не уходит.

    И с именем впереди тоже: вырезая «Джарвис», конвейер срезал хвостовую
    пунктуацию целиком, и переспрос доходил до диспетчера согласием.
    """
    for asked_back in ("Точно?", "Джарвис, точно?"):
        phone, dispatcher, pipeline, _ = _system()
        await _ask(pipeline)

        await pipeline.handle(_text(asked_back))
        await pipeline.handle(_text("да"))

        assert phone.sent == [], asked_back


# --- окно ответа живёт, пока жив вопрос ----------------------------------------


async def test_answer_window_closes_with_the_question() -> None:
    """Вопрос снят другим входом — окно у микрофона закрывается вместе с ним.

    Иначе после ответа в панели ещё полторы минуты любая речь в комнате шла
    без имени, как команда.
    """
    phone, dispatcher, pipeline, _ = _system()
    await _ask(pipeline)
    assert pipeline._follow_up_until > time.time()

    await pipeline.handle(_text("включи свет", source="keyboard"))

    assert dispatcher.awaiting is None
    assert pipeline._follow_up_until == 0.0

    await _ask(pipeline)
    await pipeline.handle(_text("да"))
    assert phone.sent == ["буду через час"]
    assert pipeline._follow_up_until == 0.0, "ответ в панели закрывает окно у микрофона"


async def test_ordinary_window_survives_an_input_from_elsewhere() -> None:
    """Окно после «Джарвис» — не окно вопроса: набранная команда его не трогает."""
    _, _, pipeline, _ = _system()
    pipeline._follow_up_until = until = time.time() + 10

    await pipeline.handle(_text("включи свет", source="keyboard"))

    assert pipeline._follow_up_until == until


# --- прерванный план продолжает только согласие ---------------------------------


async def _plan_asks(pipeline: VoicePipeline, phone: Phone) -> None:
    asked = await pipeline.handle(_text("отправь маме что я задержусь", source="voice"))
    assert asked.confirm is not None and asked.confirm.tool == "core.resume"
    assert phone.sent == []


async def test_no_then_the_same_request_asks_again() -> None:
    """«Нет» — и та же просьба ещё раз: снова вопрос, а не отправка без вопроса."""
    phone, dispatcher, pipeline, _ = _system(plan=True)
    await _plan_asks(pipeline, phone)

    await pipeline.handle(_text("нет", source="voice"))
    again = await pipeline.handle(_text("отправь маме что я задержусь", source="voice"))

    assert phone.sent == [], "отвергнутый шаг выполнился без вопроса"
    assert again.confirm is not None


async def test_stop_then_the_same_request_asks_again() -> None:
    """«Стоп» на вопрос плана — тот же отказ: повтор просьбы снова спрашивает."""
    phone, dispatcher, pipeline, _ = _system(plan=True)
    await _plan_asks(pipeline, phone)

    await pipeline.handle(_text("стоп"))
    await pipeline.handle(_text("да", source="voice"))
    again = await pipeline.handle(_text("отправь маме что я задержусь", source="voice"))

    assert phone.sent == []
    assert again.confirm is not None


async def test_question_dropped_by_another_input_takes_the_step_with_it() -> None:
    """Вопрос сбил набранный «включи свет» — повтор просьбы не исполняет шаг молча."""
    phone, dispatcher, pipeline, _ = _system(plan=True)
    await _plan_asks(pipeline, phone)

    await pipeline.handle(_text("включи свет", source="keyboard"))
    again = await pipeline.handle(_text("отправь маме что я задержусь", source="voice"))

    assert phone.sent == []
    assert again.confirm is not None


async def test_yes_still_continues_the_plan() -> None:
    """Согласие по-прежнему продолжает план с разрешённого шага."""
    phone, dispatcher, pipeline, _ = _system(plan=True)
    await _plan_asks(pipeline, phone)

    await pipeline.handle(_text("да, отправляй", source="voice"))

    assert phone.sent == ["неверный текст"]
