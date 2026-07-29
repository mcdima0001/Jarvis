"""Скилл страницы: сборка плана, проверка шагов и обучение с записью в память.

Браузера тут нет и быть не может, поэтому проверяется то, что от него не
зависит: во что превращается команда, что из этого разрешено отправлять в
страницу и сколько раз ради одного и того же спрашивается модель.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from jarvis.core.bus import LocalEventBus
from jarvis.core.config import MemoryConfig, SkillsConfig, TaskProfile
from jarvis.core.llm import LLMService, ProfileRegistry
from jarvis.core.llm.protocol import LLMRequest, LLMResponse
from jarvis.core.memory import build_memory
from jarvis.core.skills import SkillManager
from jarvis.core.tools import ToolRegistry
from jarvis.core.tts import NullTTS

_ROOT = Path(__file__).resolve().parent.parent


def _load() -> Any:
    """Загрузить скилл как модуль: он плагин и лежит вне пакета."""
    path = _ROOT / "skills" / "page" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_page", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


page = _load()


# --- домен и рецепты --------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://music.yandex.ru/home", "music.yandex.ru"),
        ("https://www.youtube.com/watch?v=1", "youtube.com"),
        ("https://m.youtube.com:443/watch", "m.youtube.com"),
        ("не ссылка", ""),
    ],
)
def test_host_of(url: str, expected: str) -> None:
    """Домен берётся без ``www`` и без порта — по нему ищется рецепт."""
    assert page.host_of(url) == expected


def test_recipe_found_by_subdomain() -> None:
    """Рецепт сайта работает и на его поддоменах: разметка там та же."""
    assert page.recipes_for("m.youtube.com", page.SITE_RECIPES)
    assert page.recipes_for("music.youtube.com", page.SITE_RECIPES)
    assert page.recipes_for("youtube.com.evil.ru", page.SITE_RECIPES) == {}
    assert page.recipes_for("", page.SITE_RECIPES) == {}


# --- что разрешено отправлять в страницу ------------------------------------


@pytest.mark.parametrize(
    "step",
    [
        {"eval": "alert(1)"},
        {"media": "shutdown"},
        {"script": ["fetch('/')"]},
        {"click": []},
        {"label": [""]},
        "не шаг",
        {},
    ],
)
def test_unknown_step_dropped(step: Any) -> None:
    """Набор шагов закрытый: чего в нём нет, того странице не отправить.

    Планы приходят из конфига, из памяти и от языковой модели — верить нельзя
    ни одному из трёх источников. То же правило, что и с названиями программ:
    выполняется только узнанное.
    """
    assert page.validate_plan([step]) == []


def test_valid_steps_survive() -> None:
    """Понятные шаги проходят как есть, повторы схлопываются."""
    plan = page.validate_plan(
        [
            {"media": "PAUSE"},
            {"click": [".ytp-next-button", ".other"]},
            {"label": ["следующий трек"]},
            {"media": "pause"},
        ]
    )
    assert plan == [
        {"media": "pause"},
        {"click": [".ytp-next-button", ".other"]},
        {"label": ["следующий трек"]},
    ]


def test_plan_is_capped() -> None:
    """Длина плана ограничена: перебирать бесконечно нечего."""
    plan = page.validate_plan([{"label": [f"кнопка {number}"]} for number in range(50)])
    assert len(plan) == page.MAX_STEPS


def test_long_selector_dropped() -> None:
    """Строки в шаге ограничены по длине — это данные, а не текст."""
    assert page.validate_plan([{"click": ["a" * (page.MAX_TEXT + 1)]}]) == []


def test_amount_lands_in_media_steps() -> None:
    """Секунды перемотки и шаг громкости подставляются в медиа-шаги."""
    plan = page.with_amount(
        [{"media": "forward"}, {"media": "louder"}, {"label": ["дальше"]}],
        seconds=30,
        step=0.2,
    )
    assert plan[0] == {"media": "forward", "seconds": 30.0}
    assert plan[1] == {"media": "louder", "amount": 0.2}
    assert plan[2] == {"label": ["дальше"]}


# --- выбор кнопки моделью ---------------------------------------------------


def test_choice_is_a_number_from_the_list() -> None:
    """Модель называет номер, а не селектор: выдумать номер она не может."""
    controls = [{"name": "Пауза", "sel": "#p"}, {"name": "Следующий трек", "sel": "#n"}]
    assert page.choose_control("2", controls)["sel"] == "#n"
    assert page.choose_control("Кнопка 1 подойдёт", controls)["name"] == "Пауза"
    assert page.choose_control("0", controls) is None
    assert page.choose_control("17", controls) is None
    assert page.choose_control("не знаю", controls) is None


def test_control_plan_keeps_label_as_backup() -> None:
    """Рядом с селектором сохраняется подпись: она переживёт редизайн."""
    plan = page.control_plan({"name": "Следующий трек", "sel": '[data-test-id="NEXT"]'})
    assert plan == [
        {"click": ['[data-test-id="NEXT"]']},
        {"label": ["следующий трек"]},
    ]


def test_control_plan_without_selector() -> None:
    """Кнопка без устойчивого селектора остаётся узнаваемой по подписи."""
    assert page.control_plan({"name": "Нравится", "sel": ""}) == [{"label": ["нравится"]}]


def test_label_variants_add_latin() -> None:
    """Услышанное сравнивается и в латинской записи: алфавит выбирает Whisper."""
    assert page.label_variants(" «Подписаться» ") == ["подписаться", "podpisatsya"]
    assert page.label_variants("Subscribe") == ["subscribe"]
    assert page.label_variants("   ") == []


# --- скилл целиком ----------------------------------------------------------

_FAKE_BROWSER = '''
"""Подделка браузера: запоминает планы и отвечает заготовками."""

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import Skill, SkillMeta
from jarvis.core.tools import tool

CALLS = []
TARGET = {"tabId": 7, "url": "https://music.yandex.ru/home", "title": "Моя волна"}
REPLIES = []
CONTROLS = []


class FakeBrowserSkill(Skill):
    """Только те инструменты, которыми пользуется скилл страницы."""

    meta = SkillMeta(name="browser", description="Подделка браузера")

    @tool(routable=False)
    async def page_target(self, site: str = "") -> ToolResult:
        """Вкладка, к которой относится команда."""
        CALLS.append(("target", site))
        return ToolResult.success(dict(TARGET))

    @tool(routable=False)
    async def page_run(self, plan: list[dict], site: str = "", tab: int = 0) -> ToolResult:
        """Выполнить план в странице."""
        CALLS.append(("run", plan, tab))
        reply = REPLIES.pop(0) if REPLIES else {"done": None}
        return ToolResult.success(dict(reply))

    @tool(routable=False)
    async def page_probe(self, site: str = "", tab: int = 0, limit: int = 40) -> ToolResult:
        """Кнопки страницы."""
        CALLS.append(("probe", tab))
        return ToolResult.success({"controls": [dict(item) for item in CONTROLS]})
'''


class _Answer:
    """Провайдер, отвечающий заранее заданной строкой."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.asked: list[str] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def configured(self) -> bool:
        return True

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.asked.append(request.messages[-1].content)
        return LLMResponse(text=self.text, model="fake")

    async def aclose(self) -> None:
        return None


@pytest.fixture
def provider() -> _Answer:
    """Модель, которая всегда выбирает вторую кнопку."""
    return _Answer("2")


@pytest.fixture
def smart_llm(provider: _Answer) -> LLMService:
    """Сервис LLM с настроенным провайдером — иначе обучение не начнётся."""
    profile = TaskProfile(task="intent", provider="fake", model="stub")
    return LLMService(
        providers={"fake": provider},
        profiles=ProfileRegistry({"intent": profile}, default_task="intent"),
    )


@pytest.fixture
def sites_memory(tmp_path: Path):
    """Память с разделом ``sites``: туда уходит выученное."""
    return build_memory(
        MemoryConfig(
            dir=tmp_path / "memory",
            documents=("profile", "sites"),
            journals=("today",),
            context_budget_tokens=500,
        )
    )


@pytest.fixture
def loaded(tmp_path: Path, sites_memory, smart_llm: LLMService):
    """Каталог с настоящим скиллом страницы и подделкой браузера рядом."""
    directory = tmp_path / "skills"
    directory.mkdir()
    shutil.copy(_ROOT / "skills" / "page" / "skill.py", directory / "page.py")
    (directory / "browser.py").write_text(_FAKE_BROWSER, encoding="utf-8")

    events = LocalEventBus()
    registry = ToolRegistry(events=events, default_timeout=2.0)
    manager = SkillManager(
        config=SkillsConfig(paths=(directory,)),
        events=events,
        tools=registry,
        memory=sites_memory,
        llm=smart_llm,
        tts=NullTTS(),
        root=tmp_path,
    )
    return manager, registry, sites_memory


async def test_pause_reaches_the_playing_tab(loaded) -> None:
    """«Пауза» уходит в найденную вкладку одним планом, без вопросов к модели."""
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.REPLIES.append({"done": "media", "detail": "пауза", "url": fake.TARGET["url"]})

    result = await registry.invoke("page.pause")

    assert result.ok
    assert result.speech_for("ru") == "Пауза."
    kinds = [call[0] for call in fake.CALLS]
    assert kinds == ["target", "run"], "лишние обращения к браузеру"
    plan = fake.CALLS[1][1]
    assert plan[0] == {"media": "pause"}, "плеер пробуется первым"
    assert fake.CALLS[1][2] == 7, "работаем в найденной вкладке, а не в какой попало"
    await manager.stop()


async def test_site_recipe_goes_first(loaded) -> None:
    """На знакомом сайте первым пробуется его рецепт, а не общий способ."""
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.REPLIES.append({"done": "click", "detail": "Следующий трек"})

    result = await registry.invoke("page.next_track")

    assert result.ok
    plan = fake.CALLS[1][1]
    assert plan[0] == {"click": ['[data-test-id="NEXT_TRACK_BUTTON"]']}
    assert {"label": page.ACTIONS["next"][0]["label"]} in plan, "общий способ остаётся запасным"
    await manager.stop()


async def test_learned_once_and_remembered(loaded, provider: _Answer) -> None:
    """Не сработало — спрашиваем модель один раз, дальше берём из памяти."""
    manager, registry, memory = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.CONTROLS[:] = [
        {"name": "Пауза", "sel": "#pause"},
        {"name": "Мне нравится", "sel": '[data-test-id="LIKE"]'},
    ]
    # Первый план не сработал, а нажатие выбранной кнопки — да.
    fake.REPLIES[:] = [{"done": None}, {"done": "click", "detail": "Мне нравится"}]

    first = await registry.invoke("page.like")

    assert first.ok
    assert [call[0] for call in fake.CALLS] == ["target", "run", "probe", "run"]
    assert "Мне нравится" in provider.asked[0], "модели показывают кнопки страницы"

    saved = await memory.documents.get("sites", "music.yandex.ru")
    assert saved["actions"]["like"] == [
        {"click": ['[data-test-id="LIKE"]']},
        {"label": ["мне нравится"]},
    ]

    # Второй раз тот же вопрос модели не задаётся: способ уже известен.
    fake.CALLS.clear()
    provider.asked.clear()
    fake.REPLIES[:] = [{"done": "click", "detail": "Мне нравится"}]

    second = await registry.invoke("page.like")

    assert second.ok
    assert [call[0] for call in fake.CALLS] == ["target", "run"]
    assert provider.asked == []
    assert fake.CALLS[1][1][0] == {"click": ['[data-test-id="LIKE"]']}
    await manager.stop()


async def test_nothing_worked_is_an_honest_refusal(loaded) -> None:
    """Ни один способ не подошёл и модель не помогла — честный отказ."""
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.CONTROLS.clear()
    fake.REPLIES.clear()

    result = await registry.invoke("page.press", {"control": "Подписаться"})

    assert not result.ok
    assert "music.yandex.ru" in result.error
    await manager.stop()


async def test_press_uses_spoken_label(loaded) -> None:
    """«Нажми подписаться» ищет кнопку по подписи, а не по рецепту."""
    manager, registry, _ = loaded
    await manager.start()
    fake = sys.modules["jarvis_skills.browser"]
    fake.CALLS.clear()
    fake.REPLIES[:] = [{"done": "label", "detail": "подписаться"}]

    result = await registry.invoke("page.press", {"control": "Подписаться"})

    assert result.ok
    assert fake.CALLS[1][1] == [{"label": ["подписаться", "podpisatsya"]}]
    await manager.stop()
