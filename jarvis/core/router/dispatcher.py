"""Диспетчер: реплика -> намерение -> вызов инструмента -> ответ.

Разделение намеренное: роутер занимается пониманием (NLU), диспетчер —
исполнением. Заменить любую из половин можно, не трогая вторую.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from jarvis.core.bus import EventBus
from jarvis.core.contracts import AssistantReplied, Intent, ToolResult, Utterance
from jarvis.core.errors import ToolNotFound
from jarvis.core.pending import Pending, answer, pick
from jarvis.core.tools import ToolRegistry

from .resolvers import LearnedResolver
from .router import Router

if TYPE_CHECKING:  # только для типов — зависимости не создаём
    from jarvis.core.situation import Situation
    from jarvis.core.verify import Checker

logger = logging.getLogger(__name__)

#: Чем разделяют две команды в одной фразе: «включи музыку **и** сделай громче».
#: Пробелы обязательны — иначе разделителем станет «и» внутри слова.
_AND = (" и ", " а также ", " and ")

#: Сколько команд принимаем в одной фразе. Три — уже предел здравого смысла:
#: длинная цепочка чаще означает, что «и» стоит внутри аргумента, а не между
#: командами.
_MAX_CHAIN = 3

#: Резолверы, которых не спрашивают при проверке гипотезы о цепочке.
#:
#: `llm` — потому что проверка обязана быть бесплатной: гипотеза «тут две
#: команды» подтверждается не всегда, и платить за каждую неудачную догадку
#: значило бы сделать союз «и» дороже, чем он стоит.
#:
#: `fallback` — потому что он отвечает **всегда**, отправляя реплику в
#: свободный разговор. С ним любая половина считалась бы командой, и фраза
#: «включи трек Я сошла с ума и не помню» разрезалась бы посередине названия.
_HYPOTHESIS_SKIPS = frozenset({"llm", "fallback"})
#: Фраза без имени, на которой детектор сработал посреди, — модель не спрашиваем:
#: её догадку ниже всё равно отбросили бы. 30.09.2026, 21:52: «Всё же лучше, чем
#: ничего» из аниме ушло в модель, три секунды ожидания дали «Минуту», а потом
#: ответа не было вовсе. Плюс четыре тысячи токенов впустую.
#:
#: И не только модель: без имени выполняется лишь узнанное шаблоном или
#: выученным (аудит 01.10.2026). `plan` отдаёт фразу в ту же модель с правом
#: действовать, `verbatim` печатает её в активное окно, а `alias`, `loose` и
#: `similar` угадывают по похожести — строчка песни «Набери мой номер»
#: впечатывалась в открытую игру. Окна ответа это не касается: сказанное в нём
#: после «Джарвис» или после вопроса приходит с именем (`Utterance.named`).
_UNNAMED_SKIPS = frozenset({"llm", "plan", "verbatim", "alias", "loose", "similar"})

_NOT_UNDERSTOOD = {
    "ru": "Не понял команду. Повтори, пожалуйста, другими словами.",
    "en": "Sorry, I didn't catch that. Could you rephrase?",
}

#: «Попробуй ещё раз»: повторяет прошлую реплику диспетчер, а не сам инструмент.
REPEAT_TOOL = "core.repeat"
#: Сколько секунд прошлая команда считается той, которую просят повторить.
#: «Ещё раз» через полчаса — это уже не про неё: повторится забытое.
REPEAT_WINDOW_S = 120.0
#: Свободный разговор. Реплике без имени он не положен (см. `Utterance.named`).
CHAT_TOOL = "core.chat"
#: Фоновое поручение: агентный цикл на минуты и за деньги — без имени не берём.
LATER_TOOL = "core.later"
#: Резолвер модели: его догадке реплика без имени не доверяется.
LLM_RESOLVER = "llm"
#: Агентный цикл: туда уходит угаданное, не сошедшееся с просьбой.
PLAN_TOOL = "core.plan"

#: Ответ на отказ от подтверждения. Короткий намеренно: человек сказал «нет»,
#: и обсуждать тут нечего.
_DROPPED = {
    "ru": ("Хорошо, отменил.", "Понял, не делаю.", "Как скажешь."),
    "en": ("All right, cancelled.", "Understood, skipping it."),
}

_NOTHING_TO_REPEAT = {"ru": "Повторять пока нечего.", "en": "There's nothing to repeat yet."}


@dataclass(frozen=True, slots=True)
class _Said:
    """Команда, которую можно повторить: как сказана, откуда и когда.

    Отдельно от обстановки (`Situation.last`), потому что задачи разные: модели
    хватает начала просьбы и одной на всех, а повтору нужен текст целиком и
    своя команда у каждого входа.
    """

    text: str
    named: bool
    at: float


class Dispatcher:
    """Проводит реплику через роутер и реестр инструментов."""

    def __init__(
        self,
        *,
        router: Router,
        registry: ToolRegistry,
        events: EventBus | None = None,
        learner: LearnedResolver | None = None,
        situation: "Situation | None" = None,
        checker: "Checker | None" = None,
    ) -> None:
        self._router = router
        self._registry = registry
        self._events = events
        #: Кому отдавать удачные разборы моделью на запоминание.
        self._learner = learner
        #: Куда записывать «что просили в прошлый раз». Диспетчер тут
        #: единственный уместный: он один знает и намерение, и чем всё кончилось.
        self._situation = situation
        #: Глаза для проверки угаданного моделью (`jarvis.core.verify`). Нет —
        #: проверки нет, всё как раньше.
        self._checker = checker
        #: Заданный вопрос, ждущий ответа. Единственное состояние между
        #: репликами во всей системе, и живёт оно здесь по той же причине:
        #: диспетчер — единственный, через кого проходит **каждая** реплика,
        #: откуда бы она ни пришла.
        self._pending: Pending | None = None
        #: Кому сказать, что вопрос снят без согласия. План держит под вопрос
        #: прерванную работу и разрешённый шаг; не узнай он об отказе, «нет»
        #: отменяло бы только реплику, а шаг ждал бы следующего вызова плана.
        self._drop_listeners: list[Callable[[Pending], None]] = []
        #: Что повторит «попробуй ещё раз» — своё у каждого входа: набранный в
        #: чате триггер не должен подменять собой сорвавшуюся голосовую команду.
        self._again: dict[str, _Said] = {}

    async def forget_unknown(self) -> tuple[str, ...]:
        """Вычистить выученное, ведущее на исчезнувшие инструменты.

        Зовётся из `JarvisApp.start` после загрузки скиллов: до неё реестр
        неполон, и уборка снесла бы живые записи.
        """
        if self._learner is None:
            return ()
        return await self._learner.forget_unknown()

    def _remember(self, utterance: Utterance, tool: str, ok: bool, *, answer: bool = False) -> None:
        """Отметить команду в обстановке для следующего разбора.

        :param answer: реплика — ответ на вопрос («да», «второй»). Командой она
            не была: в обстановке остаётся текст просьбы, а исход — того, что
            по ней сделали, и повторять «попробуй ещё раз» будет просьбу.
            Иначе повтор сорвавшейся отправки прогонял слово «да» в разговор.
        """
        if not answer:
            self._again[utterance.source] = _Said(
                text=utterance.text, named=utterance.named, at=time.monotonic()
            )
        if self._situation is None:
            return
        asked = self._situation.last if answer else None
        self._situation.command(asked.text if asked else utterance.text, tool=tool, ok=ok)

    def _split(self, utterance: Utterance) -> list[Utterance]:
        """Разрезать реплику по союзу на отдельные команды.

        Пустой список означает «резать не по чему».
        """
        text = utterance.cleaned
        lowered = text.lower()
        for word in _AND:
            if word not in lowered:
                continue
            pieces = [piece.strip(" ,") for piece in re.split(word, text, flags=re.I)]
            if len(pieces) < 2 or len(pieces) > _MAX_CHAIN or not all(pieces):
                return []
            return [
                Utterance(
                    text=piece,
                    language=utterance.language,
                    confidence=utterance.confidence,
                    source=utterance.source,
                )
                for piece in pieces
            ]
        return []

    async def _chain(self, utterance: Utterance) -> list[Intent] | None:
        """Проверить, что реплика — это две команды через союз.

        **Гипотеза подтверждается, только если каждая половина опознана как
        команда**, и это главное правило всей затеи. Союз «и» живёт не только
        между командами, но и внутри них: «включи трек Я сошла с ума и не
        помню», «напиши маме буду через час и куплю хлеб». Разрезать такое
        значит выполнить половину названия как приказ.

        Отличить одно от другого просто: у настоящей команды есть инструмент, а
        хвост названия уходит в свободный разговор. Поэтому `fallback` при
        проверке не спрашивают — он согласился бы на что угодно.

        Модель тоже не спрашивают: проверка обязана быть бесплатной. Отсюда
        плата за простоту — цепочка работает для команд, которые узнаются
        шаблонами или уже выучены. Незнакомая формулировка пойдёт обычным
        путём, целиком, как и раньше.
        """
        parts = self._split(utterance)
        if not parts:
            return None

        intents: list[Intent] = []
        for part in parts:
            intent = await self._router.route(part, without=_HYPOTHESIS_SKIPS)
            if intent is None:
                logger.debug("Цепочка не сложилась: %r не команда", part.text)
                return None
            intents.append(intent)
        return intents

    async def _run_chain(
        self, utterance: Utterance, intents: list[Intent]
    ) -> ToolResult:
        """Выполнить команды по очереди и ответить одной репликой.

        **Сорвалась первая — вторую не делаем.** «Переключись на музыку и
        включи трек» после неудачного переключения включило бы трек неизвестно
        где; человек, говоря такое, подразумевает порядок, а не два независимых
        поручения.

        Реплика одна на всю цепочку, и берётся она от последней удачной
        команды: слушать подряд «готово, готово» утомительно, а знать надо
        главное — дошло ли дело до конца.
        """
        logger.info(
            "Цепочка из %d команд: %s",
            len(intents),
            " + ".join(intent.tool for intent in intents),
        )
        result = ToolResult.failure("Пустая цепочка", tool="")
        for number, intent in enumerate(intents, start=1):
            try:
                result = await self._registry.invoke(intent.tool, intent.arguments)
            except ToolNotFound as exc:
                logger.error("Роутер выбрал несуществующий инструмент: %s", exc)
                self._remember(utterance, intent.tool, False)
                return ToolResult.failure(
                    str(exc), tool=intent.tool, speech=_NOT_UNDERSTOOD
                )

            self._remember(utterance, intent.tool, result.ok)
            self._note_question(utterance, result)
            if not result.ok or result.confirm is not None:
                # Вопрос обрывает цепочку так же, как неудача: продолжать, не
                # дождавшись ответа, значило бы выполнить остаток вслепую.
                logger.info(
                    "Цепочка прервана на %d-й команде (%s): %s",
                    number,
                    intent.tool,
                    result.error or "жду подтверждения",
                )
                return self._voiced(utterance, result)

        return self._voiced(utterance, result)

    @property
    def awaiting(self) -> Pending | None:
        """Вопрос, на который ждут ответа. Пусто — ничего не ждём."""
        return self._pending

    def ask_again(self, question: Pending) -> None:
        """Вернуть вопрос, заданный до перезапуска (`jarvis.core.restart`).

        Срок годности у вопроса по стенным часам, поэтому протухший вернётся
        протухшим и ответ на него ничего не выполнит.
        """
        self._replace(question)

    def on_drop(self, listener: Callable[[Pending], None]) -> None:
        """Подписаться на снятие вопроса без согласия.

        Снятием считается всё, кроме «да» и выбора варианта: отказ, «стоп»,
        протухание, реплика, которая не ответ, — с любого входа. Слушатель
        получает снятый вопрос и сам решает, его ли это вопрос.
        """
        self._drop_listeners.append(listener)

    def _drop(self, question: Pending) -> None:
        """Сообщить подписчикам, что на этот вопрос «да» уже не придёт."""
        for listener in self._drop_listeners:
            try:
                listener(question)
            except Exception:
                logger.exception("Подписчик на снятие вопроса упал")

    def _replace(self, question: Pending | None) -> None:
        """Поставить новый вопрос на место прежнего; прежний снимается."""
        previous, self._pending = self._pending, question
        if previous is not None and previous is not question:
            self._drop(previous)

    def decline(self, utterance: Utterance) -> ToolResult | None:
        """Снять заданный вопрос отказом, не разбирая реплику.

        Для «стоп» и «хватит», сказанных в ответ на «Отправить маме?»: конвейер
        перехватывает их как просьбу замолчать, но на вопрос это ещё и «нет».
        Без этого вопрос висел полторы минуты с открытым окном ответа, и любое
        «да» в комнате выполняло то, от чего владелец только что отказался
        (аудит 01.10.2026).

        :return: отказ, если было что отклонять; ``None`` — вопроса нет.
        """
        question = self._pending
        if question is None:
            return None
        self._replace(None)
        if not question.alive():
            logger.info("Вопрос про %s протух, отклонять нечего", question.intent.tool)
            return None
        logger.info("Владелец отказался от %s словом %r", question.intent.tool, utterance.text)
        value = {"chosen": None} if question.choices else {"confirmed": False, "tool": question.intent.tool}
        return self._voiced(utterance, ToolResult.success(value, speech=_DROPPED))

    async def _settle(self, utterance: Utterance) -> ToolResult | None:
        """Прочитать реплику как ответ на заданный вопрос.

        :return: результат, если реплика оказалась ответом; иначе ``None`` — и
            тогда она идёт обычным путём.

        **Вопрос снимается в любом случае**, даже если ответом реплика не
        оказалась. Висящий вопрос опаснее забытого: сказанное через минуту «да»
        по другому поводу выполнило бы то, о чём никто уже не помнит. Снятие
        без согласия сообщается подписчикам (`on_drop`), согласие — нет: его
        исполняет сам вопрос.
        """
        question = self._pending
        if question is None:
            return None
        self._pending = None

        if not question.alive():
            logger.info("Вопрос про %s протух, ответа не жду", question.intent.tool)
            self._drop(question)
            return None

        if question.choices:
            chosen = pick(utterance.text, len(question.choices))
            if chosen is None:
                logger.info("Реплика %r не выбор — снимаю вопрос", utterance.text)
                self._drop(question)
                return None
            if chosen is False:
                logger.info("Владелец не выбрал ничего")
                self._drop(question)
                return ToolResult.success({"chosen": None}, speech=_DROPPED)
            intent = question.choices[chosen]
            logger.info("Владелец выбрал %d: %s", chosen + 1, intent.tool)
            return await self._call(utterance, intent, answer=True)

        said = answer(utterance.text)
        if said is None:
            logger.info("Реплика %r не ответ — снимаю вопрос", utterance.text)
            self._drop(question)
            return None

        if not said:
            logger.info("Владелец отказался от %s", question.intent.tool)
            self._drop(question)
            return ToolResult.success(
                {"confirmed": False, "tool": question.intent.tool}, speech=_DROPPED
            )

        # Согласие и есть разрешение: дальше всё идёт ровно так же, как если бы
        # эту команду сказали вслух с самого начала.
        logger.info("Владелец подтвердил %s", question.intent.tool)
        return await self._call(utterance, question.intent, answer=True)

    def _note_question(self, utterance: Utterance, result: ToolResult) -> None:
        """Запомнить вопрос, если инструмент его задал."""
        if result.choices:
            self._replace(Pending.about(
                result.choices[0].intent,
                question=result.speech_for(utterance.language) or "",
                language=utterance.language or "ru",
                choices=tuple(choice.intent for choice in result.choices),
            ))
            logger.info("Жду выбора из %d вариантов", len(result.choices))
            return
        if result.confirm is None:
            return
        self._replace(Pending.about(
            result.confirm,
            question=result.speech_for(utterance.language) or "",
            language=utterance.language or "ru",
        ))
        logger.info("Жду подтверждения на %s", result.confirm.tool)

    async def _call(self, utterance: Utterance, intent: Intent, *, answer: bool = False) -> ToolResult:
        """Выполнить намерение и разобраться с последствиями.

        Общее место для обычного разбора и для подтверждённого шага: иначе
        «запомнить вопрос» и «отметить команду в обстановке» пришлось бы писать
        дважды, и однажды они разъехались бы.

        :param answer: намерение пришло ответом на вопрос, а не командой.
        """
        try:
            result = await self._registry.invoke(intent.tool, intent.arguments)
        except ToolNotFound as exc:
            logger.error("Роутер выбрал несуществующий инструмент: %s", exc)
            self._remember(utterance, intent.tool, False, answer=answer)
            return ToolResult.failure(str(exc), tool=intent.tool, speech=_NOT_UNDERSTOOD)

        self._remember(utterance, intent.tool, result.ok, answer=answer)
        self._note_question(utterance, result)
        return result

    async def handle(self, utterance: Utterance) -> ToolResult:
        """Обработать реплику целиком и вернуть результат."""
        settled = await self._settle(utterance)
        if settled is not None:
            return self._voiced(utterance, settled)

        # Цепочку без имени не режем: каждая половина узнаётся и нечёткими
        # резолверами, а их догадке реплика без имени не доверяется.
        chain = await self._chain(utterance) if utterance.named else None
        if chain is not None:
            return await self._run_chain(utterance, chain)

        intent = await self._router.route(utterance, without=frozenset() if utterance.named else _UNNAMED_SKIPS)
        if intent is None and not utterance.named:
            # Без имени и не узнано шаблоном — чужая речь: промолчать, а не «не понял».
            logger.info("Без имени, и шаблоны не узнали — не отвечаю: %r", utterance.text)
            return ToolResult.success({"ignored": "без имени, не узнано"}, tool="")
        if intent is None:
            self._remember(utterance, "", False)
            return ToolResult.failure(
                "Намерение не распознано",
                tool="",
                speech=_NOT_UNDERSTOOD,
            )

        # Имени в тексте нет, детектор услышал его посреди фразы, и командой она
        # не оказалась. Разметка 14.09.2026: так прошли «Алесса, люблю тебя» и
        # «Перестин, скорей, перчим»; из настоящих — одно исковерканное «как дела».
        # Команды без имени по-прежнему выполняются, если их узнал шаблон или
        # выученное.
        if not utterance.named and intent.tool == CHAT_TOOL:
            logger.info("Без имени, и это не команда — не отвечаю: %r", utterance.text)
            return ToolResult.success({"ignored": "без имени в свободный разговор"}, tool="")
        # То же, но разобранное моделью: к любой болтовне она подберёт инструмент.
        # Разметка 15.09.2026: «Тут реально есть другой десктоп, покинь» и
        # «Channel» ушли в план и справку. Модель для такой реплики теперь и не
        # спрашивают (`_UNNAMED_SKIPS`); проверка осталась на случай, если
        # резолвер модели назовут иначе в конфиге.
        if not utterance.named and intent.resolver == LLM_RESOLVER:
            logger.info("Без имени, и команду угадывала модель — не отвечаю: %r", utterance.text)
            return ToolResult.success({"ignored": "без имени, разобрано моделью"}, tool="")
        # «Давай ещё раз» из песни повторило бы прошлую команду, «займись …» из
        # сериала запустило бы агентный цикл на минуты (аудит 01.10.2026): оба
        # узнаются шаблоном, но сами по себе ничего не значат — только как
        # обращение к ассистенту.
        if not utterance.named and intent.tool in (REPEAT_TOOL, LATER_TOOL):
            logger.info("Без имени, а это повтор или поручение — не выполняю: %r", utterance.text)
            return ToolResult.success({"ignored": "без имени, повтор или поручение"}, tool="")

        if intent.tool == REPEAT_TOOL:
            return await self._repeat(utterance)

        watched = self._watched(intent)
        before = await self._checker.snapshot() if watched and self._checker else ""
        result = await self._call(utterance, intent)
        verified = True
        if watched and result.ok:
            result, verified = await self._verified(utterance, intent, result, before)

        # Модель разобрала фразу, инструмент отработал — связка проверена
        # делом, и со второго раза она обойдётся без модели. Записывается
        # только **подтверждённый** успех: до 25.09.2026 выучивался любой «ок»
        # инструмента, и ложный успех («статья про Тверь» → главная Википедии)
        # повторялся бы дальше бесплатно и вечно.
        if self._learner is not None and result.ok and verified and intent.resolver == "llm":
            await self._learner.remember(utterance.text, intent)

        return self._voiced(utterance, result)

    def _watched(self, intent: Intent) -> bool:
        """Проверять ли глазами: угадала модель, и результат виден на экране.

        Шаблоны и выученное — проверенная дорога, платить за их проверку
        незачем. Инструменты без видимого результата (погода, курс) отвечают
        сами за себя.
        """
        if self._checker is None or intent.resolver != LLM_RESOLVER:
            return False
        found = self._registry.get(intent.tool)
        return found is not None and found.spec.shows and self._checker.able

    async def _verified(
        self, utterance: Utterance, intent: Intent, result: ToolResult, before: str
    ) -> tuple[ToolResult, bool]:
        """Посмотреть, что вышло, и не сошлось — отдать работу плану.

        Стенд 25.09.2026: из 20 просьб без своего скилла 9 кончились ложным
        успехом, и чаще всего это был один угаданный инструмент, согласившийся
        на похожее: главная Википедии вместо статьи, вкладка ютуба вместо
        подписок. План на такое и рассчитан — сделать ещё шаг, глядя на
        результат предыдущего.
        """
        assert self._checker is not None
        seen = await self._checker.settled(before)
        said = result.speech_for(utterance.language) or ""
        verdict = await self._checker.judge(
            goal=utterance.text, did=f"{intent.tool} — {said}".strip(" —"),
            seen=seen, language=utterance.language,
        )
        if verdict.ok:
            return result, True
        if intent.tool != PLAN_TOOL and self._registry.has(PLAN_TOOL):
            logger.info("Угаданное не сошлось с просьбой — передаю плану: %s", verdict.reason)
            goal = (
                f"{utterance.text}\n\nПервая попытка ({intent.tool}) цели не достигла: "
                f"{verdict.reason}. Сейчас на экране: {seen}"
            )
            planned = await self._registry.invoke(
                PLAN_TOOL, {"goal": goal, "language": utterance.language}
            )
            # План мог упереться в необратимое и спросить «Делать?»: вопрос
            # запоминается так же, как на обычном пути, иначе «да» ушло бы в
            # роутер новой командой и цель брошена (аудит 01.10.2026).
            self._note_question(utterance, planned)
            return planned, False
        return ToolResult.failure(
            f"проверка не подтвердила: {verdict.reason}",
            tool=intent.tool,
            speech={
                "ru": f"Не уверен, что вышло: {verdict.reason}.",
                "en": f"I'm not sure it worked: {verdict.reason}.",
            },
        ), False

    async def _repeat(self, utterance: Utterance) -> ToolResult:
        """Провести прошлую реплику заново — тем же путём, что и сказанную вслух.

        Сам повтор в обстановку не пишется: иначе второе «попробуй ещё раз»
        повторяло бы само себя. Повторяется текст, а не намерение: если прошлый
        разбор был неверным, у второго есть шанс оказаться правильным.

        Повторяется **своя** команда этого входа, целиком и только недавняя
        (аудит 01.10.2026): раньше бралась последняя реплика с любого входа,
        обрезанная до 80 знаков для подсказки модели, — и голосовое «ещё раз»
        повторяло набранный в чате курс рубля, а длинное сообщение уходило без
        хвоста. Ответы на вопросы сюда не попадают вовсе (`_remember`).
        """
        last = self._again.get(utterance.source)
        if last is None or not last.text:
            return ToolResult.failure("повторять нечего", tool=REPEAT_TOOL, speech=_NOTHING_TO_REPEAT)
        if time.monotonic() - last.at > REPEAT_WINDOW_S:
            logger.info("Прошлая команда %r давняя — не повторяю", last.text)
            return ToolResult.failure(
                "прошлая команда давняя", tool=REPEAT_TOOL,
                speech={
                    "ru": "Прошлая команда была давно. Скажи её ещё раз, пожалуйста.",
                    "en": "That was a while ago. Please say the command again.",
                },
            )
        logger.info("Повторяю прошлую команду: %r", last.text)
        return await self.handle(
            Utterance(text=last.text, language=utterance.language, source=utterance.source, named=last.named)
        )

    def _voiced(self, utterance: Utterance, result: ToolResult) -> ToolResult:
        """Сообщить шине, что ответ сформирован, и вернуть его как есть."""
        spoken = result.speech_for(utterance.language)
        if self._events is not None and spoken:
            self._events.emit(
                AssistantReplied(source="dispatcher", text=spoken, spoken=False)
            )
        return result

    async def handle_text(self, text: str, *, source: str = "text") -> ToolResult:
        """Удобная обёртка для текстовой команды (режим ``--say``, Telegram)."""
        return await self.handle(Utterance(text=text, source=source))
