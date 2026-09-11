"""Диспетчер: реплика -> намерение -> вызов инструмента -> ответ.

Разделение намеренное: роутер занимается пониманием (NLU), диспетчер —
исполнением. Заменить любую из половин можно, не трогая вторую.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from jarvis.core.bus import EventBus
from jarvis.core.contracts import AssistantReplied, Intent, ToolResult, Utterance
from jarvis.core.errors import ToolNotFound
from jarvis.core.pending import Pending, answer
from jarvis.core.tools import ToolRegistry

from .resolvers import LearnedResolver
from .router import Router

if TYPE_CHECKING:  # только для типов — зависимости не создаём
    from jarvis.core.situation import Situation

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

_NOT_UNDERSTOOD = {
    "ru": "Не понял команду. Повтори, пожалуйста, другими словами.",
    "en": "Sorry, I didn't catch that. Could you rephrase?",
}

#: Ответ на отказ от подтверждения. Короткий намеренно: человек сказал «нет»,
#: и обсуждать тут нечего.
_DROPPED = {
    "ru": ("Хорошо, отменил.", "Понял, не делаю.", "Как скажешь."),
    "en": ("All right, cancelled.", "Understood, skipping it."),
}


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
    ) -> None:
        self._router = router
        self._registry = registry
        self._events = events
        #: Кому отдавать удачные разборы моделью на запоминание.
        self._learner = learner
        #: Куда записывать «что просили в прошлый раз». Диспетчер тут
        #: единственный уместный: он один знает и намерение, и чем всё кончилось.
        self._situation = situation
        #: Заданный вопрос, ждущий ответа. Единственное состояние между
        #: репликами во всей системе, и живёт оно здесь по той же причине:
        #: диспетчер — единственный, через кого проходит **каждая** реплика,
        #: откуда бы она ни пришла.
        self._pending: Pending | None = None

    async def forget_unknown(self) -> tuple[str, ...]:
        """Вычистить выученное, ведущее на исчезнувшие инструменты.

        Зовётся из `JarvisApp.start` после загрузки скиллов: до неё реестр
        неполон, и уборка снесла бы живые записи.
        """
        if self._learner is None:
            return ()
        return await self._learner.forget_unknown()

    def _remember(self, utterance: Utterance, tool: str, ok: bool) -> None:
        """Отметить команду в обстановке для следующего разбора."""
        if self._situation is not None:
            self._situation.command(utterance.text, tool=tool, ok=ok)

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

    async def _settle(self, utterance: Utterance) -> ToolResult | None:
        """Прочитать реплику как ответ на заданный вопрос.

        :return: результат, если реплика оказалась ответом; иначе ``None`` — и
            тогда она идёт обычным путём.

        **Вопрос снимается в любом случае**, даже если ответом реплика не
        оказалась. Висящий вопрос опаснее забытого: сказанное через минуту «да»
        по другому поводу выполнило бы то, о чём никто уже не помнит.
        """
        question = self._pending
        if question is None:
            return None
        self._pending = None

        if not question.alive():
            logger.info("Вопрос про %s протух, ответа не жду", question.intent.tool)
            return None

        said = answer(utterance.text)
        if said is None:
            logger.info("Реплика %r не ответ — снимаю вопрос", utterance.text)
            return None

        if not said:
            logger.info("Владелец отказался от %s", question.intent.tool)
            return ToolResult.success(
                {"confirmed": False, "tool": question.intent.tool}, speech=_DROPPED
            )

        # Согласие и есть разрешение: дальше всё идёт ровно так же, как если бы
        # эту команду сказали вслух с самого начала.
        logger.info("Владелец подтвердил %s", question.intent.tool)
        return await self._call(utterance, question.intent)

    def _note_question(self, utterance: Utterance, result: ToolResult) -> None:
        """Запомнить вопрос, если инструмент его задал."""
        if result.confirm is None:
            return
        self._pending = Pending.about(
            result.confirm,
            question=result.speech_for(utterance.language) or "",
            language=utterance.language or "ru",
        )
        logger.info("Жду подтверждения на %s", result.confirm.tool)

    async def _call(self, utterance: Utterance, intent: Intent) -> ToolResult:
        """Выполнить намерение и разобраться с последствиями.

        Общее место для обычного разбора и для подтверждённого шага: иначе
        «запомнить вопрос» и «отметить команду в обстановке» пришлось бы писать
        дважды, и однажды они разъехались бы.
        """
        try:
            result = await self._registry.invoke(intent.tool, intent.arguments)
        except ToolNotFound as exc:
            logger.error("Роутер выбрал несуществующий инструмент: %s", exc)
            self._remember(utterance, intent.tool, False)
            return ToolResult.failure(str(exc), tool=intent.tool, speech=_NOT_UNDERSTOOD)

        self._remember(utterance, intent.tool, result.ok)
        self._note_question(utterance, result)
        return result

    async def handle(self, utterance: Utterance) -> ToolResult:
        """Обработать реплику целиком и вернуть результат."""
        settled = await self._settle(utterance)
        if settled is not None:
            return self._voiced(utterance, settled)

        chain = await self._chain(utterance)
        if chain is not None:
            return await self._run_chain(utterance, chain)

        intent = await self._router.route(utterance)
        if intent is None:
            self._remember(utterance, "", False)
            return ToolResult.failure(
                "Намерение не распознано",
                tool="",
                speech=_NOT_UNDERSTOOD,
            )

        result = await self._call(utterance, intent)

        # Модель разобрала фразу, инструмент отработал — связка проверена
        # делом, и со второго раза она обойдётся без модели. Записывается
        # только успех: закрепить промах хуже, чем не выучить ничего.
        if self._learner is not None and result.ok and intent.resolver == "llm":
            await self._learner.remember(utterance.text, intent)

        return self._voiced(utterance, result)

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
