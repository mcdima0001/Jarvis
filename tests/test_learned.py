"""Выученные формулировки: запоминание удачных разборов и отмена по команде.

Проверяется то, ради чего это сделано: со второго раза та же фраза не доходит
до модели, ошибочный разбор в память не попадает, а «не сохраняй в память»
отменяет последнюю запись.
"""

from __future__ import annotations

import time

from pathlib import Path

import pytest

from jarvis.core.config import MemoryConfig
from jarvis.core.contracts import Intent, ToolResult, Utterance
from jarvis.core.memory import build_memory
from jarvis.core.router import Dispatcher, LearnedResolver, PhraseResolver, Router
from jarvis.core.router.resolvers.learned import generalize, normalize
from jarvis.core.tools import ToolRegistry, collect_tools, tool


class Studio:
    """Скилл-заглушка: одна фраза объявлена, остальное разбирает модель."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    @tool(phrases=["включи свет"])
    async def light(self) -> ToolResult:
        """Включить свет."""
        return ToolResult.success(True, speech="Свет включён.")

    @tool()
    async def press(self, control: str = "") -> ToolResult:
        """Нажать кнопку.

        :param control: что нажать.
        """
        self.calls.append({"control": control})
        return ToolResult.success(control, speech="Нажал.")

    @tool()
    async def broken(self) -> ToolResult:
        """Инструмент, который всегда отказывает."""
        return ToolResult.failure("не вышло")


class OneShotLLM:
    """Резолвер, который разбирает первую фразу и больше не нужен."""

    def __init__(self, intent: Intent) -> None:
        self._intent = intent
        self.calls = 0

    @property
    def name(self) -> str:
        return "llm"

    async def resolve(self, utterance: Utterance) -> Intent | None:
        self.calls += 1
        return Intent(
            tool=self._intent.tool,
            arguments=self._intent.arguments,
            confidence=0.85,
            resolver="llm",
            utterance=utterance.text,
        )


@pytest.fixture
def store(tmp_path: Path):
    """Память с разделом выученных формулировок."""
    return build_memory(
        MemoryConfig(
            dir=tmp_path / "memory",
            documents=("commands",),
            journals=("today",),
            context_budget_tokens=200,
        )
    ).documents


@pytest.fixture
def studio(registry: ToolRegistry) -> Studio:
    """Реестр с зарегистрированным скиллом студии."""
    skill = Studio()
    for item in collect_tools(skill, namespace="studio"):
        registry.register(item)
    return skill


# --- нормализация и обобщение ----------------------------------------------


def test_normalize_ignores_noise() -> None:
    """Регистр, лишние пробелы и знаки на краях ничего не значат."""
    assert normalize("  Включи   ТРЕТЬЕ видео! ") == "включи третье видео"
    assert normalize("«Заблокируй ноутбук».") == "заблокируй ноутбук"


def test_short_argument_stays_in_the_phrase() -> None:
    """Одно своё слово — слишком широкий шаблон, чтобы его выводить.

    «Включи {control}» поймало бы и «включи свет», и «включи кондиционер».
    Такую фразу запоминаем целиком, как услышали.
    """
    key, arguments = generalize("включи третье видео", {"control": "третье видео"})

    assert key == "включи третье видео"
    assert arguments == {"control": "третье видео"}


def test_long_enough_phrase_becomes_a_template() -> None:
    """Своих слов хватает — выучиваем сразу целое семейство фраз."""
    key, arguments = generalize(
        "поставь на паузу ютуб на большом экране", {"site": "ютуб на большом экране"}
    )

    assert key == "поставь на паузу {site}"
    assert arguments == {"site": "{site}"}


def test_argument_not_heard_in_the_phrase() -> None:
    """Значения не было в речи — обобщать нечего."""
    key, arguments = generalize("сделай там паузу", {"action": "pause"})

    assert key == "сделай там паузу"
    assert arguments == {"action": "pause"}


# --- обучение через диспетчер ----------------------------------------------


def _dispatcher(registry: ToolRegistry, learner: LearnedResolver, llm) -> Dispatcher:
    """Цепочка «фразы → выученное → модель» с обучением после успеха."""
    router = Router([PhraseResolver(registry), learner, llm], threshold=0.6)
    return Dispatcher(router=router, registry=registry, learner=learner)


async def test_successful_guess_is_learned(registry: ToolRegistry, studio: Studio, store) -> None:
    """Разобралось моделью и сработало — со второго раза модель не нужна."""
    learner = LearnedResolver(store)
    llm = OneShotLLM(Intent(tool="studio.press", arguments={"control": "третье видео"}))
    dispatcher = _dispatcher(registry, learner, llm)

    first = await dispatcher.handle_text("включи третье видео")
    assert first.ok
    assert llm.calls == 1

    second = await dispatcher.handle_text("Включи третье видео")
    assert second.ok
    assert llm.calls == 1, "второй раз до модели доходить не должно"
    assert studio.calls == [{"control": "третье видео"}, {"control": "третье видео"}]

    saved = await store.read("commands")
    assert saved["включи третье видео"]["tool"] == "studio.press"


async def test_failure_is_not_learned(registry: ToolRegistry, studio: Studio, store) -> None:
    """Промах в память не попадает: закрепить ошибку хуже, чем не выучить."""
    learner = LearnedResolver(store)
    llm = OneShotLLM(Intent(tool="studio.broken"))
    dispatcher = _dispatcher(registry, learner, llm)

    result = await dispatcher.handle_text("сделай что-то невозможное")

    assert not result.ok
    assert await store.read("commands") == {}


async def test_declared_phrase_wins_over_learned(
    registry: ToolRegistry, studio: Studio, store
) -> None:
    """Написанное в скилле руками главнее выученного."""
    learner = LearnedResolver(store)
    await learner.remember("включи свет", Intent(tool="studio.press", arguments={}))
    llm = OneShotLLM(Intent(tool="studio.broken"))
    dispatcher = _dispatcher(registry, learner, llm)

    result = await dispatcher.handle_text("включи свет")

    assert result.tool == "studio.light"


async def test_template_matches_a_family(registry: ToolRegistry, studio: Studio, store) -> None:
    """Выученный шаблон срабатывает на новых словах в том же месте."""
    learner = LearnedResolver(store)
    llm = OneShotLLM(
        Intent(tool="studio.press", arguments={"control": "кнопку подписаться"})
    )
    dispatcher = _dispatcher(registry, learner, llm)

    await dispatcher.handle_text("нажми пожалуйста кнопку подписаться")
    assert llm.calls == 1

    await dispatcher.handle_text("нажми пожалуйста кнопку поделиться")

    assert llm.calls == 1, "шаблон должен покрыть и другую кнопку"
    assert studio.calls[-1] == {"control": "кнопку поделиться"}


async def test_forget_removes_the_last_lesson(store) -> None:
    """«Не сохраняй в память» отменяет именно последнюю запись."""
    learner = LearnedResolver(store)
    await learner.remember("включи третье видео", Intent(tool="studio.press", arguments={}))
    await learner.remember("заблокируй ноутбук", Intent(tool="studio.light", arguments={}))

    forgotten = await learner.reject()

    assert forgotten == "заблокируй ноутбук"
    saved = await store.read("commands")
    assert "включи третье видео" in saved
    # Запись остаётся, но уже как «эти слова — не про это».
    assert saved["заблокируй ноутбук"] == {"rejected": ["studio.light"]}
    assert await learner.resolve(Utterance(text="заблокируй ноутбук")) is None

    # Второй раз забывать уже нечего — молча, без выдумок.
    assert await learner.reject() == ""


async def test_rejected_tool_is_not_offered_again(store) -> None:
    """Отвергнутое не выучивается заново и подсказывается модели.

    Без этого отмена была бы бессмысленной: модель предложила бы то же самое,
    разбор прошёл бы «удачно», и связка вернулась бы в память.
    """
    learner = LearnedResolver(store)
    await learner.remember("включи третье видео", Intent(tool="studio.press", arguments={}))
    await learner.reject()

    assert await learner.rejected_for("Включи третье видео!") == ("studio.press",)
    assert await learner.remember("включи третье видео", Intent(tool="studio.press")) == ""

    # А другой инструмент для той же фразы выучить можно — это и есть «попробуй
    # по-другому».
    assert await learner.remember("включи третье видео", Intent(tool="studio.light"))
    intent = await learner.resolve(Utterance(text="включи третье видео"))
    assert intent is not None and intent.tool == "studio.light"


async def test_rejection_survives_a_learned_hit(registry: ToolRegistry, studio: Studio, store) -> None:
    """Отменить можно и то, что выучено давно, а сработало сегодня.

    Промах чаще всего вылезает не в момент обучения, а при повторе: вчера
    запомнили, сегодня повторили — и оказалось не то.
    """
    learner = LearnedResolver(store)
    await learner.remember("включи третье видео", Intent(tool="studio.press", arguments={}))
    learner._last = ""  # как после перезапуска: в памяти есть, в сеансе — нет

    assert await learner.resolve(Utterance(text="включи третье видео")) is not None
    assert await learner.reject() == "включи третье видео"


async def test_forget_last_tool_covers_skills(registry: ToolRegistry, store) -> None:
    """Общая команда отмены доходит и до скиллов, которые учатся сами.

    Договорённость об имени `forget_last` нужна ровно за этим: скилл `page`
    запоминает кнопки сайтов, и «не сохраняй в память» должно отменять и это.
    """
    from jarvis.core.builtin import CoreTools

    class Learning:
        """Скилл, который умеет забывать последнее выученное."""

        def __init__(self) -> None:
            self.asked = 0

        @tool(routable=False)
        async def forget_last(self, apply: bool = True) -> ToolResult:
            """Забыть последнее.

            :param apply: False — только сказать, что и когда, ничего не стирая.
            """
            if not apply:
                return ToolResult.success(
                    {"what": "кнопку лайка на music.yandex.ru", "at": time.time()}
                )
            self.asked += 1
            return ToolResult.success("кнопку лайка на music.yandex.ru")

    skill = Learning()
    for item in collect_tools(skill, namespace="page"):
        registry.register(item)

    learner = LearnedResolver(store)
    await learner.remember("включи третье видео", Intent(tool="studio.press", arguments={}))

    core = CoreTools(
        llm=None,  # type: ignore[arg-type]
        memory=None,  # type: ignore[arg-type]
        registry=registry,
        skills=None,  # type: ignore[arg-type]
        learner=learner,
    )
    for item in collect_tools(core, namespace="core"):
        registry.register(item)

    result = await registry.invoke("core.forget_last")

    assert result.ok
    assert skill.asked == 1
    assert "включи третье видео" in result.value
    assert "кнопку лайка на music.yandex.ru" in result.value


async def test_nothing_to_forget_is_honest(registry: ToolRegistry, store) -> None:
    """Нечего забывать — так и говорим, а не делаем вид."""
    from jarvis.core.builtin import CoreTools

    core = CoreTools(
        llm=None,  # type: ignore[arg-type]
        memory=None,  # type: ignore[arg-type]
        registry=registry,
        skills=None,  # type: ignore[arg-type]
        learner=LearnedResolver(store),
    )
    for item in collect_tools(core, namespace="core"):
        registry.register(item)

    result = await registry.invoke("core.forget_last")

    assert result.ok
    assert result.value == []
    assert result.speech_for("ru") == "Мне нечего забывать."


async def test_missing_memory_section_only_warns(tmp_path: Path) -> None:
    """Нет раздела в конфиге — обучение молча выключается, а не роняет команду."""
    documents = build_memory(
        MemoryConfig(
            dir=tmp_path / "memory",
            documents=("profile",),
            journals=("today",),
            context_budget_tokens=200,
        )
    ).documents
    learner = LearnedResolver(documents)

    assert await learner.resolve(Utterance(text="включи третье видео")) is None
    assert await learner.remember("включи третье видео", Intent(tool="studio.press")) == ""


async def test_forget_touches_only_the_latest(registry: ToolRegistry, store) -> None:
    """Отменяется одно событие, а не по записи у каждого, кто учится.

    Живой случай 01.08.2026: владелец сказал «не сохраняй в память», отменяя
    разбор фразы сорокасекундной давности, — и вместе с ней слетел верный
    рецепт кнопки, выученный девятью минутами раньше. «Последнее» у каждого
    было своё, а команда отменяла все «последние» разом.
    """
    from jarvis.core.builtin import CoreTools

    class Learning:
        """Скилл, который запомнил кнопку давно и с тех пор ничего не делал."""

        def __init__(self) -> None:
            self.asked = 0

        @tool(routable=False)
        async def forget_last(self, apply: bool = True) -> ToolResult:
            """Забыть последнее.

            :param apply: False — только сказать, что и когда, ничего не стирая.
            """
            if not apply:
                return ToolResult.success(
                    {"what": "next на music.yandex.ru", "at": time.time() - 9 * 60}
                )
            self.asked += 1
            return ToolResult.success("next на music.yandex.ru")

    skill = Learning()
    for item in collect_tools(skill, namespace="page"):
        registry.register(item)

    learner = LearnedResolver(store)
    await learner.remember("так, говорите больше", Intent(tool="core.be_brief", arguments={}))

    core = CoreTools(
        llm=None,  # type: ignore[arg-type]
        memory=None,  # type: ignore[arg-type]
        registry=registry,
        skills=None,  # type: ignore[arg-type]
        learner=learner,
    )
    for item in collect_tools(core, namespace="core"):
        registry.register(item)

    result = await registry.invoke("core.forget_last")

    assert result.ok
    assert result.value == ["так, говорите больше"]
    assert skill.asked == 0, "давнее нажатие к этой отмене отношения не имеет"


async def test_one_command_can_teach_two_things(registry: ToolRegistry, store) -> None:
    """А вот записанное в те же секунды отменяется вместе.

    «Нажми лайк» разбирается моделью и тут же учит и формулировку, и кнопку —
    это одно событие, и отменять его надо целиком.
    """
    from jarvis.core.builtin import CoreTools

    class Learning:
        def __init__(self) -> None:
            self.asked = 0

        @tool(routable=False)
        async def forget_last(self, apply: bool = True) -> ToolResult:
            """Забыть последнее.

            :param apply: False — только сказать, что и когда, ничего не стирая.
            """
            if not apply:
                return ToolResult.success({"what": "like на music.yandex.ru", "at": time.time()})
            self.asked += 1
            return ToolResult.success("like на music.yandex.ru")

    skill = Learning()
    for item in collect_tools(skill, namespace="page"):
        registry.register(item)

    learner = LearnedResolver(store)
    await learner.remember("поставь сердечко", Intent(tool="page.like", arguments={}))

    core = CoreTools(
        llm=None,  # type: ignore[arg-type]
        memory=None,  # type: ignore[arg-type]
        registry=registry,
        skills=None,  # type: ignore[arg-type]
        learner=learner,
    )
    for item in collect_tools(core, namespace="core"):
        registry.register(item)

    result = await registry.invoke("core.forget_last")

    assert skill.asked == 1
    assert sorted(result.value) == ["like на music.yandex.ru", "поставь сердечко"]


async def test_tool_without_apply_is_named_in_the_log(
    registry: ToolRegistry, store, caplog
) -> None:
    """Скилл со старым `forget_last` пропускается — но не молча.

    Молчаливый пропуск означал бы, что «не запоминай» перестало работать для
    целого скилла, а по логу этого не видно.
    """
    from jarvis.core.builtin import CoreTools

    class Old:
        @tool(routable=False)
        async def forget_last(self) -> ToolResult:
            """Забыть последнее — по-старому, без apply."""
            return ToolResult.success("что-то")

    for item in collect_tools(Old(), namespace="legacy"):
        registry.register(item)

    core = CoreTools(
        llm=None,  # type: ignore[arg-type]
        memory=None,  # type: ignore[arg-type]
        registry=registry,
        skills=None,  # type: ignore[arg-type]
        learner=LearnedResolver(store),
    )
    for item in collect_tools(core, namespace="core"):
        registry.register(item)

    with caplog.at_level("WARNING"):
        await registry.invoke("core.forget_last")

    assert "legacy.forget_last" in caplog.text
    assert "apply" in caplog.text
