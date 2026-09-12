"""Составная просьба узнаётся до шаблонов.

Живой запуск 12.09.2026: «найди инстаграм этой девушки на экране» ушло в
`search.web_search` с этой самой фразой в качестве запроса, и ассистент ответил
«Нашёл 3. Первый: Акиньшина, Оксана Сергеевна». На экран он не посмотрел ни
разу — шаблон `найди {query}` забрал фразу целиком, до модели она не дожила.

Проверяется главное свойство этой проверки: **она молчит в сомнении**. Лишнее
срабатывание уводит бесплатную команду в модель, а это ровно то, на чём проект
экономит.
"""

from __future__ import annotations

import pytest

from jarvis.core.contracts import Utterance
from jarvis.core.router import PlanResolver
from jarvis.core.router.resolvers.plan import needs_a_plan

# --- что считается составным -------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "найди инстаграм этой девушки на экране",
        "найди место где сделана эта фотография",
        "найди точное место где была сделана фотография на экране",
        "открой в гугл картах место с этой фотографии",
        "отправь маме фото с экрана",
        "скачай картинку с экрана",
        "переведи то что на экране",
    ],
)
def test_two_things_at_once_need_a_plan(text: str) -> None:
    """Просьба ссылается на невиденное и просит с ним что-то сделать."""
    assert needs_a_plan(text)


@pytest.mark.parametrize(
    "text",
    [
        # Одного взгляда достаточно — это обычный однократный вызов.
        "что на экране",
        "что это за самолет на экране",
        "прочитай что на экране",
        "посмотри на экран",
        "где была сделана эта фотка на экране",
        "какой авиакомпании этот самолёт",
        # Действие есть, а смотреть не на что.
        "найди рецепт борща",
        "открой браузер",
        "открой папку загрузки",
        "напиши маме буду через час",
        # Типовые команды обязаны остаться бесплатными.
        "курс доллара",
        "сделай громче",
        "",
    ],
)
def test_ordinary_requests_stay_free(text: str) -> None:
    """Одного признака мало: иначе бесплатные команды уедут в модель.

    Это и есть цена ошибки в другую сторону. Шаблоны закрывают четыре сотни
    фраз без сети и без денег, и терять их из-за жадной проверки нельзя.
    """
    assert not needs_a_plan(text)


def test_pressing_a_button_on_the_page_is_not_a_plan() -> None:
    """«Найди на экране кнопку оплатить» — это нажатие, им занимается страница."""
    assert not needs_a_plan("найди на экране кнопку оплатить")
    assert not needs_a_plan("нажми ссылку на экране")


def test_action_is_matched_as_a_whole_word() -> None:
    """Действие узнаётся словом, а не куском другого слова."""
    assert needs_a_plan("Найди, пожалуйста, эту девушку на экране")
    assert not needs_a_plan("что за принайди на экране")


# --- во что это превращается -------------------------------------------------


async def test_whole_request_goes_into_the_plan() -> None:
    """В цель уходит фраза целиком: в отрезанном лежит вторая половина дела."""
    said = "найди инстаграм этой девушки на экране"

    intent = await PlanResolver().resolve(Utterance(text=said, language="ru"))

    assert intent is not None
    assert intent.tool == "core.plan"
    assert intent.arguments == {"goal": said}
    assert intent.resolver == "plan"


async def test_plain_request_is_left_to_the_others() -> None:
    """Обычная команда резолвер не трогает — дальше по цепочке."""
    assert await PlanResolver().resolve(Utterance(text="курс доллара")) is None
    assert await PlanResolver().resolve(Utterance(text="")) is None


def test_plan_stands_before_the_templates_in_the_shipped_config() -> None:
    """Порядок и есть всё решение: после шаблонов эта проверка бесполезна."""
    from jarvis.core.config import load_config

    order = load_config().router.resolvers
    assert "plan" in order, "резолвер не включён в рабочем конфиге"
    assert order.index("plan") < order.index("phrase")
