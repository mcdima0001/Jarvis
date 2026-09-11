"""Переспросить и понять ответ: единственное состояние между репликами.

Разбор ответа проверяется строго, и это главное здесь. Ошибка в сторону «не
понял» стоит лишнего вопроса; ошибка в другую сторону отправляет сообщение не
тому человеку. Поэтому всё, что не опознано уверенно, ответом не считается.
"""

from __future__ import annotations

import time

from jarvis.core.bus import LocalEventBus
from jarvis.core.contracts import Intent, ToolResult, Utterance
from jarvis.core.pending import TTL, Pending, answer
from jarvis.core.router import Dispatcher, PhraseResolver, Router
from jarvis.core.tools import ToolRegistry, collect_tools, tool


class Studio:
    """Инструмент, который спрашивает, и тот, о котором спрашивают."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    @tool(phrases=["напиши маме"], reversible=True)
    async def compose(self) -> ToolResult:
        """Собрать сообщение и спросить разрешения."""
        return ToolResult.asking(
            Intent(tool="studio.send", arguments={"text": "буду через час"}),
            question="Отправить маме «буду через час»?",
        )

    @tool(reversible=False)
    async def send(self, text: str = "") -> ToolResult:
        """Отправить сообщение."""
        self.sent.append(text)
        return ToolResult.success({"sent": text}, speech="Отправил.")

    @tool(phrases=["включи свет"], reversible=True)
    async def light(self) -> ToolResult:
        """Включить свет."""
        return ToolResult.success(True, speech="Свет включён.")


def _dispatcher(events: LocalEventBus) -> tuple[Studio, Dispatcher]:
    """Диспетчер с этими инструментами и обычным разбором по фразам."""
    registry = ToolRegistry(events=events, default_timeout=1.0)
    skill = Studio()
    for item in collect_tools(skill, namespace="studio"):
        registry.register(item)
    router = Router([PhraseResolver(registry)], threshold=0.6)
    return skill, Dispatcher(router=router, registry=registry, events=events)


def _said(text: str) -> Utterance:
    """Реплика от человека."""
    return Utterance(text=text, source="text")


# --- разбор ответа ----------------------------------------------------------


def test_plain_agreement_is_understood() -> None:
    """Согласие говорят по-разному, и все эти формы одинаковы."""
    for word in ("да", "Да!", "ага", "давай", "конечно", "ок", "yes", "go ahead"):
        assert answer(word) is True, word


def test_plain_refusal_is_understood() -> None:
    """Отказ тоже."""
    for word in ("нет", "не надо", "отмена", "стоп", "no", "cancel", "never mind"):
        assert answer(word) is False, word


def test_refusal_wins_over_a_polite_start() -> None:
    """«Конечно нет» — это отказ, хотя начинается со слова согласия.

    Порядок проверок в разборе переставлять нельзя ровно поэтому.
    """
    assert answer("конечно нет") is False
    assert answer("да не надо") is False


def test_half_agreement_is_not_an_answer() -> None:
    """«Да, но сначала…» ответом не считается: это уже другая мысль."""
    assert answer("да но сначала подумай") is None
    assert answer("а что там") is None


def test_any_negation_cancels_even_inside_agreement() -> None:
    """Отрицание где угодно в короткой реплике читается как отказ.

    «Да, если не сложно» по-человечески согласие, а разбор вернёт отказ.
    Перекос намеренный: лишняя отмена стоит одного повторения, а лишняя
    отправка — сообщения не тому человеку. Формулировка редкая, цена ошибки
    несимметричная.
    """
    assert answer("да если не сложно") is False


def test_a_command_is_never_mistaken_for_an_answer() -> None:
    """Обычная команда не должна случайно оказаться согласием."""
    assert answer("включи свет") is None
    assert answer("поставь музыку на паузу") is None
    assert answer("") is None


def test_long_reply_is_not_an_answer() -> None:
    """На «да или нет» пятью словами не отвечают, а командуют — запросто."""
    assert answer("да да да да да да да") is None


def test_question_has_a_shelf_life() -> None:
    """Протухший вопрос перестаёт существовать, а не срабатывает молча."""
    question = Pending.about(Intent(tool="x"), ttl=TTL)
    assert question.alive()
    assert not question.alive(now=time.time() + TTL + 1)


# --- согласие выполняется ---------------------------------------------------


async def test_yes_performs_exactly_what_was_asked(events: LocalEventBus) -> None:
    """Согласие исполняется теми же аргументами, о которых спрашивали.

    Иначе «отправить маме?» — «да» отправило бы неизвестно что.
    """
    skill, dispatcher = _dispatcher(events)

    asked = await dispatcher.handle(_said("напиши маме"))
    assert asked.confirm is not None
    assert dispatcher.awaiting is not None
    assert not skill.sent

    done = await dispatcher.handle(_said("да"))
    assert done.ok
    assert skill.sent == ["буду через час"]
    assert dispatcher.awaiting is None


async def test_no_cancels_and_performs_nothing(events: LocalEventBus) -> None:
    """Отказ снимает вопрос и ничего не делает."""
    skill, dispatcher = _dispatcher(events)

    await dispatcher.handle(_said("напиши маме"))
    done = await dispatcher.handle(_said("нет"))

    assert done.ok
    assert done.value == {"confirmed": False, "tool": "studio.send"}
    assert not skill.sent
    assert dispatcher.awaiting is None


async def test_unrelated_reply_drops_the_question_and_is_routed(
    events: LocalEventBus,
) -> None:
    """Реплика, которая не ответ, снимает вопрос и идёт обычным путём.

    Висящий вопрос опаснее забытого: сказанное через минуту «да» по другому
    поводу выполнило бы то, о чём никто уже не помнит.
    """
    skill, dispatcher = _dispatcher(events)

    await dispatcher.handle(_said("напиши маме"))
    other = await dispatcher.handle(_said("включи свет"))

    assert other.ok
    assert other.tool == "studio.light"
    assert not skill.sent
    assert dispatcher.awaiting is None


async def test_expired_question_is_not_answered(events: LocalEventBus) -> None:
    """На протухший вопрос «да» уже не действует."""
    skill, dispatcher = _dispatcher(events)

    await dispatcher.handle(_said("напиши маме"))
    stale = dispatcher.awaiting
    assert stale is not None
    # Вопрос, заданный давно: срок вышел, пока владелец занимался другим.
    dispatcher._pending = Pending(
        intent=stale.intent, question=stale.question, until=time.time() - 1
    )

    reply = await dispatcher.handle(_said("да"))

    assert not skill.sent
    # Реплика ушла обычным путём и командой не оказалась.
    assert not reply.ok
    assert dispatcher.awaiting is None


async def test_question_is_asked_only_once(events: LocalEventBus) -> None:
    """Ответ снимает вопрос: второе «да» уже ничего не отправит."""
    skill, dispatcher = _dispatcher(events)

    await dispatcher.handle(_said("напиши маме"))
    await dispatcher.handle(_said("да"))
    await dispatcher.handle(_said("да"))

    assert skill.sent == ["буду через час"]


async def test_question_breaks_a_chain(events: LocalEventBus) -> None:
    """Цепочка через союз останавливается на вопросе.

    Продолжать, не дождавшись ответа, значило бы выполнить остаток вслепую.
    """
    skill, dispatcher = _dispatcher(events)

    result = await dispatcher.handle(_said("напиши маме и включи свет"))

    assert result.confirm is not None
    assert dispatcher.awaiting is not None
    assert not skill.sent
