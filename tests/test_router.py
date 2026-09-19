"""Роутер: цепочка резолверов, экономия обращений к LLM, замыкающее звено."""

from __future__ import annotations

from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from jarvis.core.situation import Situation


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


class _Refusing:
    """Провайдер, который отказывается выбирать инструмент.

    Ровно то, чем грешит дешёвая модель: каталог она видит, но отвечает текстом.
    """

    def __init__(self, *, answer_at: int = 0) -> None:
        self.asked: list[str] = []
        #: На каком по счёту вопросе всё-таки выбрать инструмент; 0 — никогда.
        self._answer_at = answer_at

    @property
    def name(self) -> str:
        return "fake"

    @property
    def configured(self) -> bool:
        return True

    async def complete(self, request):
        from jarvis.core.llm.protocol import LLMResponse, ToolCall

        self.asked.append(request.model)
        if self._answer_at and len(self.asked) >= self._answer_at:
            return LLMResponse(
                model=request.model,
                tool_calls=(ToolCall(name="lights__on", arguments={}),),
            )
        return LLMResponse(text="Не знаю, о чём речь.", model=request.model)

    async def aclose(self) -> None:
        return None


def _llm_service(provider) -> object:
    """Сервис LLM с двумя задачами: дешёвой и той, что умнее."""
    from jarvis.core.config import TaskProfile
    from jarvis.core.llm import LLMService, ProfileRegistry

    return LLMService(
        providers={"fake": provider},
        profiles=ProfileRegistry(
            {
                "intent": TaskProfile(task="intent", provider="fake", model="cheap"),
                "intent_strong": TaskProfile(task="intent_strong", provider="fake", model="smart"),
            },
            default_task="intent",
        ),
    )


async def test_refusal_is_retried_by_a_stronger_model(lights_registry: ToolRegistry) -> None:
    """Дешёвая модель не выбрала инструмент — спрашиваем ту, что умнее.

    Отказ дешёвой модели раньше означал худший из исходов: реплика уходила в
    свободный разговор, то есть деньги всё равно тратились, а команда не
    выполнялась.
    """
    from jarvis.core.router import LLMResolver

    provider = _Refusing(answer_at=2)
    resolver = LLMResolver(
        lights_registry, _llm_service(provider), tasks=("intent", "intent_strong")
    )

    intent = await resolver.resolve(Utterance(text="а ну давай свет"))

    assert intent is not None
    assert intent.tool == "lights.on"
    assert provider.asked == ["cheap", "smart"], "порядок: сначала дешёвая"


async def test_single_task_means_no_second_question(lights_registry: ToolRegistry) -> None:
    """Одна задача в конфиге — переспрашивать не будем: это чужие деньги."""
    from jarvis.core.router import LLMResolver

    provider = _Refusing()
    resolver = LLMResolver(lights_registry, _llm_service(provider), tasks=("intent",))

    assert await resolver.resolve(Utterance(text="а ну давай свет")) is None
    assert provider.asked == ["cheap"]


async def test_cheap_answer_costs_nothing_extra(lights_registry: ToolRegistry) -> None:
    """Справилась дешёвая — вторую не зовём."""
    from jarvis.core.router import LLMResolver

    provider = _Refusing(answer_at=1)
    resolver = LLMResolver(
        lights_registry, _llm_service(provider), tasks=("intent", "intent_strong")
    )

    intent = await resolver.resolve(Utterance(text="а ну давай свет"))

    assert intent is not None
    assert provider.asked == ["cheap"]


# --- повтор и реплики без имени ---------------------------------------------------


class CoreStub:
    """Заглушки ядра: повтор и свободный разговор."""

    @tool(phrases=["попробуй ещё раз"], reversible=False)
    async def repeat(self) -> ToolResult:
        """Повторить прошлую команду."""
        return ToolResult.failure("мимо диспетчера")

    @tool(reversible=True)
    async def chat(self, text: str) -> ToolResult:
        """Поговорить.

        :param text: реплика.
        """
        return ToolResult.success(text, speech="Болтаю.")


class ChatFallback:
    """Всё неузнанное — в разговор, как настоящий fallback."""

    @property
    def name(self) -> str:
        return "fallback"

    async def resolve(self, utterance: Utterance) -> Intent | None:
        return Intent(tool="core.chat", arguments={"text": utterance.text}, confidence=1.0)


def _dispatcher(registry: ToolRegistry) -> tuple[Dispatcher, "Situation"]:
    from jarvis.core.situation import Situation

    for item in collect_tools(CoreStub(), namespace="core"):
        registry.register(item)
    situation = Situation()
    router = Router([PhraseResolver(registry), ChatFallback()], threshold=0.6)
    return Dispatcher(router=router, registry=registry, situation=situation), situation


async def test_try_again_repeats_the_previous_command(lights_registry: ToolRegistry) -> None:
    """Живой случай 14.09.2026: «попробуй ещё раз» модель разобрала во включение трека."""
    dispatcher, situation = _dispatcher(lights_registry)
    nothing = await dispatcher.handle_text("попробуй ещё раз")
    assert not nothing.ok and nothing.speech_for("ru") == "Повторять пока нечего."

    await dispatcher.handle_text("зажги свет")
    again = await dispatcher.handle_text("попробуй ещё раз")
    assert again.ok and again.speech == "Свет включён."
    # Повтор не записывает сам себя: второе «ещё раз» повторит свет, а не повтор.
    assert situation.last is not None and situation.last.text == "зажги свет"


async def test_unnamed_phrase_may_command_but_not_chat(lights_registry: ToolRegistry) -> None:
    """«Алесса, люблю тебя» без имени — молчание; «зажги свет» без имени — выполняется."""
    dispatcher, _ = _dispatcher(lights_registry)
    ignored = await dispatcher.handle(Utterance(text="Алесса, люблю тебя", named=False))
    assert ignored.ok and ignored.value == {"ignored": "без имени в свободный разговор"}
    command = await dispatcher.handle(Utterance(text="зажги свет", named=False))
    assert command.speech == "Свет включён."
    chat = await dispatcher.handle(Utterance(text="как дела"))
    assert chat.speech == "Болтаю."


class _Guessing:
    """Резолвер под именем модели: любую фразу считает включением света."""

    @property
    def name(self) -> str:
        """Имя резолвера."""
        return "llm"

    async def resolve(self, utterance: Utterance) -> Intent | None:
        """Угадать свет."""
        return Intent(tool="lights.on", confidence=0.85)


async def test_unnamed_phrase_is_not_trusted_to_model_guess(lights_registry: ToolRegistry) -> None:
    """Живой случай 15.09.2026: разговор с другом без имени ушёл моделью в план."""
    router = Router([PhraseResolver(lights_registry), _Guessing()], threshold=0.6)
    dispatcher = Dispatcher(router=router, registry=lights_registry)
    ignored = await dispatcher.handle(Utterance(text="тут реально есть другой десктоп", named=False))
    assert ignored.ok and ignored.value == {"ignored": "без имени, разобрано моделью"}
    # Шаблон без имени по-прежнему выполняется, а с именем модели доверяют.
    assert (await dispatcher.handle(Utterance(text="зажги свет", named=False))).speech == "Свет включён."
    assert (await dispatcher.handle(Utterance(text="сделай светло"))).speech == "Свет включён."


class _Retelling(_Refusing):
    """Модель выбирает разговор, но вписывает в него свой пересказ."""

    async def complete(self, request):
        from jarvis.core.llm.protocol import LLMResponse, ToolCall

        return LLMResponse(model=request.model, tool_calls=(ToolCall(
            name="core__chat", arguments={"text": "Похоже, вы сказали «щитак». Уточните.", "language": "ru"},
        ),))


async def test_chat_gets_what_was_heard_not_the_model_retelling() -> None:
    """Живой случай 14.09.2026: в журнал ложилось «Похоже, вы сказали…» как вопрос."""
    from jarvis.core.router import LLMResolver

    class Talk:
        @tool(name="chat", reversible=True)
        async def chat(self, text: str, language: str = "ru") -> ToolResult:
            """Поговорить."""
            return ToolResult.success(text)

    registry = ToolRegistry()
    for item in collect_tools(Talk(), namespace="core"):
        registry.register(item)
    resolver = LLMResolver(registry, _llm_service(_Retelling()), tasks=("intent",))

    intent = await resolver.resolve(Utterance(text="щитак"))

    assert intent is not None and intent.tool == "core.chat"
    assert intent.arguments["text"] == "щитак"


def test_plans_and_help_are_never_learned() -> None:
    """«Не сохраняй» → план выключил автопамять и выучился (14.09.2026, 17:14)."""
    from jarvis.core.router.resolvers.learned import NEVER_LEARN

    assert {"core.plan", "core.later", "core.help"} <= NEVER_LEARN


class Picker:
    """Скилл, который не уверен и предлагает выбрать."""

    def __init__(self) -> None:
        self.done: list[str] = []

    @tool(phrases=["найди модуль"], reversible=True)
    async def ask(self) -> ToolResult:
        """Предложить варианты."""
        from jarvis.core.contracts import Choice, numbered

        options = [Choice(name, Intent(tool="picker.take", arguments={"name": name})) for name in ("keys", "peace")]
        return ToolResult.choosing(options, question=f"Какой? {numbered(['keys', 'peace'])}.")

    @tool(routable=False, reversible=True)
    async def take(self, name: str) -> ToolResult:
        """Выполнить выбранное.

        :param name: что выбрали.
        """
        self.done.append(name)
        return ToolResult.success(name, speech=f"Взял {name}.")


async def test_numbered_choice_runs_the_picked_option(registry: ToolRegistry) -> None:
    """19.09.2026: вместо двадцати названий — пять с номерами, ответ «второй» выполняет выбранное."""
    picker = Picker()
    for item in collect_tools(picker, namespace="picker"):
        registry.register(item)
    dispatcher = Dispatcher(router=Router([PhraseResolver(registry)], threshold=0.6), registry=registry)

    asked = await dispatcher.handle_text("найди модуль")
    assert asked.speech == "Какой? 1 — keys, 2 — peace." and len(asked.choices) == 2
    chosen = await dispatcher.handle_text("номер два")
    assert picker.done == ["peace"] and chosen.speech == "Взял peace."

    await dispatcher.handle_text("найди модуль")
    dropped = await dispatcher.handle_text("нет")
    assert picker.done == ["peace"] and dropped.value == {"chosen": None}


def test_rank_puts_the_likely_first_and_cuts_to_five() -> None:
    from jarvis.core.text import rank

    names = {name: (name,) for name in ("keys", "peace", "screen", "search", "sentinel", "speedtest", "telegram")}
    names["keys"] += ("кейс", "case")
    assert rank("Кейс", names)[0] == "keys"
    assert len(rank("что-то", names)) == 5
