"""Агентный цикл: цель, шаг, результат, следующий шаг.

До этого модуля `Dispatcher` делал ровно один вызов и на этом заканчивался.
Цепочка через союз «и» циклом не была: обе команды называл вслух человек, а
система лишь исполняла их по очереди. Здесь другое — **модель видит результат
собственного шага и решает, что делать дальше**. Без этого «найди тот трек, что
играл вчера вечером, и поставь» невыполнимо в принципе, сколько скиллов ни пиши.

**Место — ядро, а не скилл**, хотя правило проекта велит сомневаться. Цикл не
добавляет возможностей: он не умеет ничего, чего нет в реестре. Он меняет то,
**как** выполняется уже существующее, — как роутер и диспетчер, рядом с
которыми и лежит.

**Правило «план не даёт новых прав» соблюдается буквально.** Каждый шаг идёт
через тот же реестр, что и голосовая команда, а необратимый шаг цикл **сам не
выполняет**: он останавливается и возвращает намерение целиком, вместе с
аргументами. Спросить о нём — дело вызывающего (`core.plan`), а разрешение
приходит голосом владельца и ничем не отличается от той же команды, сказанной
вслух с самого начала. Механика ожидания ответа живёт в `core/pending.py`.

Возвращается именно `Intent`, а не имя инструмента: согласие надо исполнять
теми же аргументами, о которых спрашивали, иначе «отправить маме?» — «да»
отправило бы неизвестно что и неизвестно кому.

**Формат переписки нарочно простой.** Родной для провайдеров способ вести
tool-calling требует хранить в сообщениях и сам вызов, и ответ на него с
идентификатором. Это расширило бы контракт `Message` ради одного места. Вместо
этого после каждого шага в переписку уходит обычная реплика «вызвал то-то,
получилось то-то»: модели этого хватает, чтобы решить следующий шаг, а
`LLMProvider` остаётся тонким.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from jarvis.core.contracts import Intent
from jarvis.core.errors import LLMError, LLMNotConfigured
from jarvis.core.llm import Message
from jarvis.core.tools import ToolRegistry

if TYPE_CHECKING:  # только для типов — зависимостей не создаём
    from jarvis.core.llm import LLMService
    from jarvis.core.situation import Situation

logger = logging.getLogger(__name__)

#: Профиль задачи в конфиге. Модель обязана уметь вызовы инструментов.
PLAN_TASK = "plan"

#: Сколько шагов позволено одной цели.
#:
#: Предел тут не формальность, а то, что удерживает счёт от разбега: **каждый
#: виток тащит с собой каталог инструментов**, то есть около полутора тысяч
#: входных токенов (замерено 11.09.2026, не прикинуто). Пять шагов стоят
#: примерно как пять неузнанных фраз; больше за одну просьбу почти наверняка
#: означает, что модель ходит по кругу, и ходит она по нему за наши деньги.
MAX_STEPS = 5

#: Инструменты, которых цикл не видит.
#:
#: `core.plan` — чтобы план не построил план: вложенность тут не даёт ничего,
#: кроме умножения расхода. `core.chat` — чтобы цикл не «выполнял» цель
#: разговором о ней: свободный разговор отвечает всегда и на что угодно, и с ним
#: любая недостижимая цель выглядела бы успешной.
#: `core.later` — по той же причине, что и `core.plan`: фоновое поручение
#: запускает такой же цикл, и план, раздающий поручения самому себе, размножился
#: бы в стороне от всякого предела шагов.
HIDDEN: frozenset[str] = frozenset({"core.plan", "core.chat", "core.later"})

#: Сколько текста результата показывать модели. Целиком нельзя: список чатов
#: или выдача поиска съест окно за два шага.
RESULT_LIMIT = 400

_SYSTEM = {
    "ru": (
        "Ты выполняешь просьбу владельца по шагам, вызывая инструменты. "
        "За один раз вызывай ровно один инструмент. После каждого шага тебе "
        "сообщат, что получилось, — решай следующий шаг по результату, а не по "
        "догадке. Уже выполненный шаг не повторяй. "
        "Когда цель достигнута или стало ясно, что её не достичь, ответь "
        "текстом без вызова: одним-двумя предложениями, их произнесут вслух."
    ),
    "en": (
        "You are carrying out the owner's request step by step by calling "
        "tools. Call exactly one tool at a time. After each step you are told "
        "what happened; decide the next step from the result, not from a guess. "
        "Never repeat a step already done. When the goal is reached, or it is "
        "clear it cannot be, reply with text and no call: one or two sentences, "
        "they will be read aloud."
    ),
}


@dataclass(frozen=True, slots=True, kw_only=True)
class Step:
    """Один выполненный шаг плана."""

    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    ok: bool = True
    summary: str = ""


def _signature(tool: str, arguments: Mapping[str, Any]) -> str:
    """Отпечаток шага: по нему цикл узнаёт, что ходит по кругу."""
    return f"{tool}:{json.dumps(dict(arguments), sort_keys=True, ensure_ascii=False)}"


@dataclass(frozen=True, slots=True, kw_only=True)
class Outcome:
    """Чем кончился цикл."""

    #: Что сказать вслух. Пусто, если цикл не дошёл до ответа.
    answer: str = ""
    steps: tuple[Step, ...] = ()
    #: Почему остановились раньше времени. Пусто — дошли сами.
    stopped: str = ""
    #: Шаг, который цикл не стал делать сам. Целиком намерением, а не одним
    #: именем: чтобы спросить «сделать это?», надо помнить и аргументы, иначе
    #: согласие пришлось бы исполнять наугад.
    blocked: Intent | None = None

    @property
    def ok(self) -> bool:
        """Дошёл ли цикл до ответа, не упёршись ни во что."""
        return not self.stopped and self.blocked is None and bool(self.answer)


class Planner:
    """Выполняет составную просьбу шагами, сверяясь с результатом."""

    def __init__(
        self,
        *,
        llm: "LLMService",
        registry: ToolRegistry,
        situation: "Situation | None" = None,
        steps: int = MAX_STEPS,
        task: str = PLAN_TASK,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._situation = situation
        self._steps = max(1, steps)
        self._task = task

    def _schemas(self) -> list[dict[str, Any]]:
        """Каталог для цикла: всё, что видит роутер, кроме спрятанного."""
        return self._registry.catalog().function_schemas(exclude=HIDDEN)

    def _opening(self, goal: str, language: str) -> list[Message]:
        """Первые две реплики: правила игры и сама цель."""
        system = _SYSTEM.get(language, _SYSTEM["ru"])
        if self._situation is not None:
            happening = self._situation.describe(language)
            if happening:
                system = f"{system}\n\nЧто происходит сейчас: {happening}"
        return [Message.system(system), Message.user(goal)]

    @staticmethod
    def _brief(value: Any, error: str | None) -> str:
        """Короткая выжимка результата — то, что увидит модель."""
        if error:
            return f"не получилось: {error}"[:RESULT_LIMIT]
        if value is None:
            return "готово"
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return text[:RESULT_LIMIT]

    async def run(
        self, goal: str, *, language: str = "ru", done: Sequence[Step] = ()
    ) -> Outcome:
        """Выполнить цель шагами и вернуть, чем всё кончилось.

        :param done: шаги, сделанные до этого вызова. Нужны, когда цикл упёрся в
            необратимое, владелец разрешил, и работу надо **продолжить**, а не
            начать заново. Без них цикл пошёл бы по второму кругу: снова выбрал
            бы тот же шаг, снова упёрся бы в него и снова спросил — а человек
            уже ответил. Их результаты идут в переписку так же, как результаты
            шагов, сделанных прямо сейчас: модель не должна различать, на каком
            вызове шаг случился.
        """
        schemas = self._schemas()
        if not schemas:
            return Outcome(stopped="нет инструментов")

        messages = self._opening(goal, language)
        history: list[Step] = list(done)
        seen: set[str] = {_signature(step.tool, step.arguments) for step in history}
        for step in history:
            messages.append(
                Message.user(f"Вызвал {step.tool}, получилось: {step.summary}")
            )

        for number in range(1, self._steps + 1):
            try:
                response = await self._llm.complete(
                    messages, task=self._task, tools=schemas, tool_choice="auto"
                )
            except (LLMError, LLMNotConfigured) as exc:
                logger.warning("Цикл оборвался на %d-м шаге: %s", number, exc)
                return Outcome(steps=tuple(history), stopped=f"модель не ответила: {exc}")

            if not response.has_tool_calls:
                # Модель считает, что дело кончено, и отвечает словами.
                return Outcome(answer=response.text.strip(), steps=tuple(history))

            call = response.tool_calls[0]
            name = self._registry.resolve_function_name(call.name)
            found = self._registry.get(name) if name else None
            if name is None or found is None:
                logger.warning("Цикл выбрал несуществующий инструмент: %s", call.name)
                return Outcome(steps=tuple(history), stopped="выбран несуществующий инструмент")

            # «План не даёт новых прав»: необратимое сам не делаю.
            if not found.spec.unattended:
                logger.info("Цикл упёрся в необратимый шаг: %s", name)
                return Outcome(
                    steps=tuple(history),
                    blocked=Intent(
                        tool=name, arguments=dict(call.arguments), resolver="plan"
                    ),
                )

            signature = _signature(name, call.arguments)
            if signature in seen:
                # Повтор того же шага означает, что модель ходит по кругу.
                # Дальше она будет ходить по нему за наши деньги.
                logger.info("Цикл повторяется на шаге %s, останавливаюсь", name)
                return Outcome(
                    answer=response.text.strip(), steps=tuple(history), stopped="шаг повторился"
                )
            seen.add(signature)

            result = await self._registry.invoke(name, dict(call.arguments))
            brief = self._brief(result.value, result.error)
            history.append(
                Step(tool=name, arguments=dict(call.arguments), ok=result.ok, summary=brief)
            )
            logger.info("Шаг %d плана: %s -> %s", number, name, "ок" if result.ok else brief)

            if not result.ok:
                # Сорвавшийся шаг обрывает план, как и в цепочке через союз:
                # человек подразумевает порядок, а не независимые поручения.
                return Outcome(steps=tuple(history), stopped=f"шаг {name} не удался: {brief}")

            messages.append(
                Message.user(
                    f"Шаг {number}: вызван {name}, результат — {brief}. "
                    f"Если цель достигнута, ответь текстом без вызова."
                )
            )

        return Outcome(steps=tuple(history), stopped="исчерпан предел шагов")
