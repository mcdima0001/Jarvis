"""Роутер: цепочка резолверов, экономия обращений к LLM, замыкающее звено."""

from __future__ import annotations

import pytest

from jarvis.core.contracts import Intent, ToolResult, Utterance
from jarvis.core.router import (
    AliasResolver,
    Dispatcher,
    FallbackResolver,
    PhraseResolver,
    Router,
)
from jarvis.core.tools import ToolRegistry, collect_tools, tool


class Lights:
    """Скилл-заглушка с фразами и шаблоном."""

    @tool(phrases=["включи свет", "зажги свет"])
    async def on(self) -> ToolResult:
        """Включить свет."""
        return ToolResult.success(True, speech="Свет включён.")

    @tool(phrases=["запомни {text}"])
    async def note(self, text: str) -> ToolResult:
        """Записать заметку.

        :param text: текст заметки.
        """
        return ToolResult.success(text)


class SpyResolver:
    """Резолвер-шпион: фиксирует, звали ли его."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        """Имя резолвера."""
        return "spy"

    async def resolve(self, utterance: Utterance) -> Intent | None:
        """Записать вызов и ничего не вернуть."""
        self.calls += 1
        return None


@pytest.fixture
def lights_registry(registry: ToolRegistry) -> ToolRegistry:
    """Реестр с зарегистрированным скиллом света."""
    for item in collect_tools(Lights(), namespace="lights"):
        registry.register(item)
    return registry


async def test_exact_phrase_resolves(lights_registry: ToolRegistry) -> None:
    """Точная фраза попадает в нужный инструмент с полной уверенностью."""
    resolver = PhraseResolver(lights_registry)
    intent = await resolver.resolve(Utterance(text="Включи свет!"))

    assert intent is not None
    assert intent.tool == "lights.on"
    assert intent.confidence == 1.0


async def test_template_preserves_argument_case(lights_registry: ToolRegistry) -> None:
    """Аргумент шаблона сохраняет исходный регистр — «XLR», а не «xlr»."""
    resolver = PhraseResolver(lights_registry)
    intent = await resolver.resolve(Utterance(text="запомни купить кабель XLR"))

    assert intent is not None
    assert intent.tool == "lights.note"
    assert intent.arguments["text"] == "купить кабель XLR"


async def test_known_phrase_never_reaches_llm(lights_registry: ToolRegistry) -> None:
    """Главное требование к экономии: типовая команда не доходит до сети."""
    spy = SpyResolver()
    router = Router([PhraseResolver(lights_registry), spy], threshold=0.6)

    intent = await router.route(Utterance(text="включи свет"))

    assert intent is not None
    assert intent.tool == "lights.on"
    assert spy.calls == 0, "фразовый резолвер справился — дальше идти не должно"


async def test_unknown_phrase_falls_through(lights_registry: ToolRegistry) -> None:
    """Незнакомая фраза доходит до следующих звеньев."""
    spy = SpyResolver()
    router = Router([PhraseResolver(lights_registry), spy], threshold=0.6)

    await router.route(Utterance(text="сделай что-нибудь странное"))

    assert spy.calls == 1


async def test_fuzzy_alias_catches_misrecognition(lights_registry: ToolRegistry) -> None:
    """Опечатка распознавания ловится нечётким сравнением, без LLM."""
    resolver = AliasResolver(lights_registry, {})
    intent = await resolver.resolve(Utterance(text="включи свет пожалуйста"))

    assert intent is None or intent.tool == "lights.on"

    exact = AliasResolver(lights_registry, {"свет давай": "lights.on"})
    aliased = await exact.resolve(Utterance(text="свет давай"))
    assert aliased is not None
    assert aliased.tool == "lights.on"


async def test_last_resolver_ignores_threshold(lights_registry: ToolRegistry) -> None:
    """Замыкающее звено принимается всегда: за ним никого нет.

    Иначе свободный диалог с низкой уверенностью терялся бы молча.
    """
    router = Router([PhraseResolver(lights_registry), FallbackResolver()], threshold=0.6)

    intent = await router.route(Utterance(text="расскажи анекдот"))

    assert intent is not None
    assert intent.tool == "core.chat"
    assert intent.confidence < 0.6


async def test_low_confidence_skipped_when_chain_continues(
    lights_registry: ToolRegistry,
) -> None:
    """Слабая догадка не обходит более сильное звено дальше по цепочке."""
    weak = FallbackResolver(tool="lights.on", confidence=0.1)
    spy = SpyResolver()
    router = Router([weak, spy, FallbackResolver()], threshold=0.6)

    intent = await router.route(Utterance(text="что-нибудь"))

    assert spy.calls == 1, "слабый резолвер не должен был закончить разбор"
    assert intent is not None
    assert intent.resolver == "fallback"


async def test_failing_resolver_does_not_break_chain(lights_registry: ToolRegistry) -> None:
    """Упавший резолвер не ломает маршрутизацию."""

    class Broken:
        @property
        def name(self) -> str:
            return "broken"

        async def resolve(self, utterance: Utterance) -> Intent | None:
            raise RuntimeError("резолвер сломался")

    router = Router([Broken(), PhraseResolver(lights_registry)], threshold=0.6)
    intent = await router.route(Utterance(text="включи свет"))

    assert intent is not None
    assert intent.tool == "lights.on"


async def test_dispatcher_runs_tool_end_to_end(lights_registry: ToolRegistry) -> None:
    """Диспетчер проводит реплику от текста до результата инструмента."""
    router = Router([PhraseResolver(lights_registry)], threshold=0.6)
    dispatcher = Dispatcher(router=router, registry=lights_registry)

    result = await dispatcher.handle_text("зажги свет")

    assert result.ok
    assert result.speech == "Свет включён."


async def test_dispatcher_reports_unresolved(lights_registry: ToolRegistry) -> None:
    """Если разобрать не удалось, пользователь получает внятный ответ."""
    router = Router([PhraseResolver(lights_registry)], threshold=0.6)
    dispatcher = Dispatcher(router=router, registry=lights_registry)

    result = await dispatcher.handle_text("абракадабра")

    assert not result.ok
    assert result.speech


class Competing:
    """Два скилла с пересекающимися шаблонами — как search и browser."""

    @tool(phrases=["найди {query}"])
    async def broad(self, query: str) -> ToolResult:
        """Найти и рассказать.

        :param query: запрос.
        """
        return ToolResult.success(f"рассказываю про {query}")

    @tool(phrases=["найди в {engine} {query}"])
    async def narrow(self, query: str, engine: str = "") -> ToolResult:
        """Открыть выдачу в браузере.

        :param query: запрос.
        :param engine: где искать.
        """
        return ToolResult.success({"engine": engine, "query": query})


async def test_specific_template_wins_over_broad_one(events) -> None:
    """Шаблон с бо́льшим числом своих слов проверяется первым.

    «Найди в гугле котиков» подходит и под «найди {query}», и под
    «найди в {engine} {query}». Побеждать должен второй, иначе исход зависел
    бы от того, какой скилл загрузился раньше, — то есть от алфавита имён
    файлов.
    """
    registry = ToolRegistry(events=events)
    for item in collect_tools(Competing(), namespace="rivals"):
        registry.register(item)

    intent = await PhraseResolver(registry).resolve(Utterance(text="найди в гугле котиков"))

    assert intent is not None
    assert intent.tool == "rivals.narrow"
    assert intent.arguments == {"engine": "гугле", "query": "котиков"}


async def test_broad_template_still_matches_its_own_phrase(events) -> None:
    """Общий шаблон продолжает работать там, где частный не подходит."""
    registry = ToolRegistry(events=events)
    for item in collect_tools(Competing(), namespace="rivals"):
        registry.register(item)

    intent = await PhraseResolver(registry).resolve(Utterance(text="найди котиков"))

    assert intent is not None
    assert intent.tool == "rivals.broad"
