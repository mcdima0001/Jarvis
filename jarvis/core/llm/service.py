"""LLMService — задачи поверх провайдеров.

Здесь живёт то, что не зависит от вендора: `ask`, `chat`, `summarize`,
`extract_intent`. Написано один раз и работает с любым провайдером, поэтому
добавление Gemini или Groq не тянет за собой копирование промптов.

Выбор модели идёт через профиль задачи (`dialog`, `summarize`, `intent`,
`plan`), а сменить модель можно на лету: `llm.set_model("dialog", "...")`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from jarvis.core.contracts import detect_language
from jarvis.core.errors import LLMError, LLMNotConfigured, LLMOutOfCredits
from jarvis.core.faults import Faults
from jarvis.core.state import BRIEF, Modes
from jarvis.core.tools import ToolCatalog

from .profiles import ProfileRegistry
from .protocol import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    StreamingProvider,
    ToolCall,
)

if TYPE_CHECKING:
    from .usage import UsageLog

logger = logging.getLogger(__name__)

#: Подсказка маршрутизатора.
#:
#: Отдельно сказано, что просьбу нужно во что-то превратить. Без этого модель
#: охотно пользуется правом «ответить текстом»: на «включи видео» она вернула
#: четыре токена вежливого отказа, реплика ушла в свободный разговор, и тот
#: бодро отчитался «включаю видео», ничего не сделав. Отказ должен оставаться
#: возможным, но только там, где он уместен — на вопросах и разговоре.
_INTENT_SYSTEM = (
    "Ты — маршрутизатор голосового ассистента, управляющего домашней студией. "
    "Определи, какой инструмент вызвать для реплики пользователя, и вызови его. "
    "Реплика в повелительном наклонении («включи», «открой», «поставь», "
    "«закрой», «убери») — это команда: выбери самый близкий по смыслу "
    "инструмент, даже если формулировка непривычная. Речь почти всегда идёт о "
    "том, что уже открыто или запущено. "
    "Отвечай текстом без вызова только если это вопрос или разговор, а не "
    "просьба что-то сделать. "
    "Если в просьбе несколько действий подряд или следующий шаг зависит от "
    "результата предыдущего — выбирай core.plan и передавай ему просьбу "
    "целиком, а не один из конкретных инструментов: тот выполнит только первую "
    "половину и промолчит об остальном."
)

#: Что дописывается к подсказке в режиме «отвечай коротко», по языку просьбы.
_BRIEF_HINT = {
    "ru": "Отвечай одним предложением: сейчас просили покороче.",
    "en": "Answer in a single sentence: brevity was requested.",
}

#: Потолок ответа в этом режиме. Просьбу уложиться в предложение модель иногда
#: не слышит, а потолок слышит всегда.
BRIEF_TOKENS = 90

_SUMMARY_SYSTEM = (
    "Ты сжимаешь текст до сути. Пиши по-русски, без вступлений и оценок, "
    "только факты, которые важны для дальнейшей работы."
)


#: Сколько не трогать мёртвого провайдера, когда запасного нет. Полминуты —
#: компромисс: меньше значит платить ожиданием почти на каждой фразе, больше —
#: не заметить, что счёт уже пополнили.
DEAD_RETRY_S = 30.0


def _out_of_money(exc: LLMError) -> bool:
    """Беда надолго: пустой счёт или нет ключа.

    Сетевой сбой и «слишком часто» сюда не идут намеренно: у запасного
    провайдера та же сеть, а на ограничение частоты он ответит так же — вторая
    попытка только удвоит ожидание перед ответом.
    """
    return isinstance(exc, (LLMOutOfCredits, LLMNotConfigured))


def _spare_model(model: str, provider: str) -> str:
    """Как та же модель зовётся у запасного провайдера.

    У OpenRouter модели OpenAI записаны с приставкой («openai/gpt-5.4-nano»), и
    это единственное, что можно угадать честно. Всё остальное задаётся
    `fallback_model` в профиле — угадывать имена моделей вслепую опаснее, чем
    промолчать.
    """
    if "/" in model or provider != "openrouter":
        return model
    return f"openai/{model}"


def _as_float(value: object) -> float:
    """Дробное число из чужого JSON — по тем же правилам, что и целое."""
    try:
        return float(value)  # type: ignore[arg-type]  # разбираем что дали
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: object) -> int:
    """Число из чужого JSON, каким бы оно оттуда ни пришло.

    Провайдеры присылают счётчики то числом, то строкой, а иногда не присылают
    вовсе. Расход — не то, ради чего стоит ронять ответ модели.
    """
    try:
        return int(value)  # type: ignore[call-overload]  # разбираем что дали
    except (TypeError, ValueError):
        return 0


@dataclass
class Spending:
    """Сколько израсходовано с момента запуска.

    Токены не бесконечные, поэтому расход виден в логе после каждого запроса и
    целиком — в `core.status`. Без счётчика любая экономия остаётся верой.
    """

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: Стоимость в долларах — её сообщает сам OpenRouter, мы не считаем.
    cost: float = 0.0
    #: Разбивка по задачам: где именно уходит.
    by_task: dict[str, int] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        """Всего токенов, вход плюс выход."""
        return self.prompt_tokens + self.completion_tokens

    def add(self, task: str, usage: Mapping[str, object]) -> None:
        """Учесть один ответ модели."""
        prompt = _as_int(usage.get("prompt_tokens"))
        completion = _as_int(usage.get("completion_tokens"))
        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.cost += _as_float(usage.get("cost"))
        self.by_task[task] = self.by_task.get(task, 0) + prompt + completion

    def summary(self) -> str:
        """Однострочный отчёт для статуса."""
        if not self.calls:
            return "модель ещё не вызывалась"
        parts = ", ".join(
            f"{task} {tokens}" for task, tokens in sorted(
                self.by_task.items(), key=lambda item: -item[1]
            )
        )
        money = f", ${self.cost:.4f}" if self.cost else ""
        return f"{self.calls} запрос(ов), {self.total_tokens} токенов{money} ({parts})"


class LLMService:
    """Задачи поверх набора провайдеров."""

    def __init__(
        self,
        *,
        providers: Mapping[str, LLMProvider],
        profiles: ProfileRegistry,
        modes: "Modes | None" = None,
        usage: "UsageLog | None" = None,
        faults: "Faults | None" = None,
        fallback_retry_min: float = 30.0,
    ) -> None:
        self._providers = dict(providers)
        self._profiles = profiles
        self._spending = Spending()
        #: Расход по дням с примерной ценой — для вкладки «Расход» в панели.
        self._usage = usage
        #: Режимы. Нужен ровно один — «отвечай коротко»: он про длину любого
        #: текста, который ассистент произносит, а производит текст не один
        #: инструмент. Место, через которое проходят все, здесь.
        self._modes = modes
        #: Журнал последнего сбоя: пустой счёт и отсутствие сети называются
        #: вслух, а не прячутся за «не справился» (21.09.2026).
        self._faults = faults if faults is not None else Faults()
        #: Провайдеры, которых временно не трогаем: у них кончились деньги или
        #: нет ключа. Имя → до какого момента (монотонные часы).
        self._blocked: dict[str, float] = {}
        #: Чем именно провайдер отказал: этим же отказываем, пока не отпустит.
        self._blocked_why: dict[str, LLMError] = {}
        self._retry_after = max(60.0, fallback_retry_min * 60)

    @property
    def faults(self) -> "Faults":
        """Последний сбой обращения к модели — чтобы объяснить неудачу вслух."""
        return self._faults

    @property
    def spending(self) -> Spending:
        """Расход токенов с момента запуска."""
        return self._spending

    @property
    def usage(self) -> "UsageLog | None":
        """Расход по дням; ``None`` — не ведётся."""
        return self._usage

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "llm"

    @property
    def profiles(self) -> ProfileRegistry:
        """Реестр профилей — смена модели во время работы."""
        return self._profiles

    @property
    def available(self) -> bool:
        """Есть ли хотя бы один настроенный провайдер."""
        return any(provider.configured for provider in self._providers.values())

    async def start(self) -> None:
        """Ничего не поднимает: клиенты создаются лениво при первом запросе."""

    async def stop(self) -> None:
        """Закрыть соединения всех провайдеров."""
        for provider in self._providers.values():
            try:
                await provider.aclose()
            except Exception:
                logger.exception("Ошибка при закрытии провайдера %s", provider.name)

    # --- управление моделями ----------------------------------------------

    def set_model(self, task: str, model: str) -> None:
        """Сменить модель для задачи во время работы."""
        self._profiles.set_model(task, model)

    def models(self) -> dict[str, str]:
        """Текущая раскладка «задача -> провайдер/модель»."""
        return self._profiles.snapshot()

    # --- низкий уровень ----------------------------------------------------

    def _provider(self, name: str) -> LLMProvider:
        """Найти провайдера по имени."""
        provider = self._providers.get(name)
        if provider is None:
            raise LLMNotConfigured(
                f"Провайдер {name!r} не найден. Доступны: "
                f"{', '.join(sorted(self._providers)) or '(ни одного)'}"
            )
        return provider

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        task: str | None = None,
        tools: Sequence[Mapping[str, object]] = (),
        tool_choice: str = "auto",
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Выполнить запрос по профилю задачи.

        :param max_tokens: предел ответа поверх профиля. Нужен режиму «отвечай
            коротко»: просьбы уложиться в предложение модель иногда не слышит,
            а потолок слышит всегда.
        """
        profile, provider, request = self._prepare(
            messages, task=task, tools=tools, tool_choice=tool_choice, max_tokens=max_tokens
        )
        if self._blocked_until(profile.provider):
            # У основного недавно кончились деньги: спрашивать его снова —
            # значит платить ожиданием на каждой фразе.
            if self._spare(profile) is not None:
                provider, request = self._to_spare(profile, request)
            else:
                # Запасного нет — но и к мёртвому идти незачем: он ответит тем
                # же отказом, только через полсекунды ожидания, и так на каждой
                # неузнанной фразе (живой лог 24.09.2026: два похода на фразу,
                # `intent` и `intent_strong`). Отказываем сразу и тем же сбоем,
                # чтобы вслух прозвучала настоящая причина.
                raise self._blocked_why[profile.provider]
        try:
            response = await provider.complete(request)
        except LLMError as exc:
            spare = self._spare(profile) if provider.name == profile.provider else None
            if _out_of_money(exc) and provider.name == profile.provider:
                # Помним беду независимо от того, есть ли запасной: ходить к
                # пустому счёту на каждой фразе не нужно в обоих случаях.
                self._block(profile.provider, exc, spare=spare is not None)
            if spare is not None and _out_of_money(exc):
                # Деньги и ключ — беды надолго, а не на секунду: уходим к
                # запасному. Сетевой сбой сюда не идёт: у запасного та же сеть,
                # и вторая попытка ничего не даст.
                provider, request = self._to_spare(profile, request)
                try:
                    response = await provider.complete(request)
                except LLMError as second:
                    self._note(second, provider)
                    raise
            else:
                self._note(exc, provider)
                raise
        self._faults.forget()
        await self._account(profile, response.usage, provider=provider.name)
        return response

    # --- запасной провайдер -------------------------------------------------

    def _note(self, exc: LLMError, provider: LLMProvider) -> None:
        """Записать сбой в журнал — все пути к модели сходятся в `complete`."""
        fault = self._faults.note(exc, provider=str(getattr(provider, "title", "") or provider.name))
        logger.warning("Модель не ответила (%s): %s", fault.kind, exc)

    def _spare(self, profile: Any) -> LLMProvider | None:
        """Запасной провайдер профиля, если он задан и собрался."""
        name = getattr(profile, "fallback_provider", "")
        if not name or name == profile.provider:
            return None
        spare = self._providers.get(name)
        if spare is None or not spare.configured:
            return None
        return spare

    def _to_spare(self, profile: Any, request: LLMRequest) -> tuple[LLMProvider, LLMRequest]:
        """Тот же запрос, но к запасному провайдеру и его моделью."""
        spare = self._spare(profile)
        assert spare is not None  # зовётся только когда он есть
        model = getattr(profile, "fallback_model", "") or _spare_model(profile.model, spare.name)
        logger.info("Спрашиваю запасного: %s/%s вместо %s/%s",
                    spare.name, model, profile.provider, profile.model)
        return spare, replace(request, model=model)

    def _block(self, name: str, exc: LLMError, *, spare: bool) -> None:
        """Не трогать провайдера некоторое время: счёт за минуту не пополнится.

        Сроки разные, и разница не формальная. **Есть запасной** — ждём долго:
        работа идёт, терять на попытках нечего. **Запасного нет** — ждём
        полминуты: ассистент сейчас беспомощен, владелец пополняет счёт прямо
        сейчас, и получить «не могу» ещё полчаса после пополнения он не должен.
        """
        wait = self._retry_after if spare else DEAD_RETRY_S
        self._blocked[name] = time.monotonic() + wait
        #: Чем отказывать, пока блокировка держится: тем же сбоем, что случился.
        #: Придумывать свой нельзя — вслух прозвучала бы неправда.
        self._blocked_why[name] = exc
        logger.warning(
            "У провайдера %s беда со счётом или ключом (%s) — не трогаю его %.0f с%s",
            name, exc, wait, " (работаю на запасном)" if spare else "",
        )

    def _blocked_until(self, name: str) -> bool:
        """Идёт ли ещё блокировка основного провайдера."""
        until = self._blocked.get(name, 0.0)
        if not until:
            return False
        if time.monotonic() >= until:
            self._blocked.pop(name, None)
            self._blocked_why.pop(name, None)
            logger.info("Пробую снова основного провайдера %s", name)
            return False
        return True

    def _prepare(
        self,
        messages: Sequence[Message],
        *,
        task: str | None,
        tools: Sequence[Mapping[str, object]] = (),
        tool_choice: str = "auto",
        max_tokens: int | None = None,
    ) -> tuple[Any, LLMProvider, LLMRequest]:
        """Профиль задачи, её провайдер и готовый запрос — общее у ответа целиком и потоком."""
        profile = self._profiles.get(task)
        provider = self._provider(profile.provider)

        payload = list(messages)
        if profile.system and not any(m.role == "system" for m in payload):
            payload.insert(0, Message.system(profile.system))

        request = LLMRequest(
            messages=payload,
            model=profile.model,
            temperature=profile.temperature,
            max_tokens=min(profile.max_tokens, max_tokens) if max_tokens else profile.max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            reasoning=profile.reasoning,
        )
        logger.debug("LLM запрос: задача=%s модель=%s", profile.task, profile.model)
        return profile, provider, request

    async def _account(self, profile: Any, usage: Mapping[str, Any], *, provider: str = "") -> None:
        """Записать расход запроса: в сеанс, в файл дня и в лог."""
        self._spending.add(profile.task, usage)
        if self._usage is not None:
            # Файл дня пишется в потоке: запись на диск в цикле событий
            # задержала бы голос ради бухгалтерии.
            await asyncio.to_thread(self._usage.add, profile.task, profile.model, usage)
        logger.info(
            "LLM %s (%s%s): %s+%s токенов%s, всего за сеанс %s",
            profile.task,
            profile.model,
            f" через {provider}" if provider and provider != profile.provider else "",
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
            f", ${float(usage['cost']):.5f}" if usage.get("cost") else "",
            self._spending.total_tokens,
        )

    # --- задачи ------------------------------------------------------------

    async def ask(
        self,
        prompt: str,
        *,
        task: str | None = None,
        system: str | None = None,
        context: str | None = None,
        history: Sequence[Message] = (),
    ) -> str:
        """Задать одиночный вопрос и получить текстовый ответ.

        :param context: заранее собранный фрагмент памяти; полную память
            передавать нельзя — только нужные разделы (см. `ContextBuilder`).
        :param history: недавние реплики разговора. Идут **перед** вопросом и
            своими ролями: роли модель понимает сама, и пересказывать «владелец
            сказал, ты ответил» значило бы платить токенами за то, что формат
            выражает бесплатно.
        """
        messages, limit = self._question(prompt, system=system, context=context, history=history)
        response = await self.complete(messages, task=task, max_tokens=limit)
        return response.text

    async def ask_stream(
        self,
        prompt: str,
        *,
        task: str | None = None,
        system: str | None = None,
        context: str | None = None,
        history: Sequence[Message] = (),
    ) -> AsyncIterator[str]:
        """То же, что `ask`, но ответ отдаётся кусками по мере написания.

        Сорвалось **до первого куска** — спрашиваем обычным запросом: у него
        свои поправки отказов (OpenAI учится на отвергнутых параметрах), и
        потерять ответ из-за потока было бы глупо. Сорвалось посреди ответа —
        ошибка идёт наверх: половину уже произнесли, и начинать заново нельзя.
        """
        messages, limit = self._question(prompt, system=system, context=context, history=history)
        profile, provider, request = self._prepare(messages, task=task, max_tokens=limit)
        if not isinstance(provider, StreamingProvider):
            yield (await self.complete(messages, task=task, max_tokens=limit)).text
            return
        usage: dict[str, Any] = {}
        started = False
        try:
            async for piece in provider.stream(request, usage):
                started = True
                yield piece
        except LLMError as exc:
            if started:
                raise
            logger.warning("Поток ответа модели не открылся (%s) — спрашиваю целиком", exc)
            yield (await self.complete(messages, task=task, max_tokens=limit)).text
            return
        await self._account(profile, usage)

    def _question(
        self,
        prompt: str,
        *,
        system: str | None,
        context: str | None,
        history: Sequence[Message],
    ) -> tuple[list[Message], int | None]:
        """Переписка для одиночного вопроса и потолок ответа под режим «коротко»."""
        messages: list[Message] = []
        brief = self._brief_line(prompt)
        if system or brief:
            messages.append(Message.system(" ".join(part for part in (system, brief) if part)))
        if context:
            messages.append(Message.system(f"Контекст:\n{context}"))
        messages.extend(history)
        messages.append(Message.user(prompt))
        return messages, BRIEF_TOKENS if brief else None

    def _brief_line(self, prompt: str) -> str:
        """Что дописать к подсказке в режиме «отвечай коротко».

        Живёт здесь, а не в инструменте, ровно по одной причине: текст вслух
        производит не один инструмент. Режим включили — и `core.chat`
        послушался, а поиск в тот же момент зачитал три предложения из
        Википедии, потому что про режим не знал (поймано на живом запуске
        01.08.2026). Место, через которое проходят все, здесь одно.

        Язык берётся у самой просьбы: русская подсказка на английском вопросе
        утащила бы и ответ в русский.
        """
        if self._modes is None or not self._modes.active(BRIEF):
            return ""
        return _BRIEF_HINT.get(detect_language(prompt, default="ru"), _BRIEF_HINT["ru"])

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        task: str | None = None,
    ) -> str:
        """Продолжить диалог и вернуть текст ответа."""
        response = await self.complete(messages, task=task)
        return response.text

    async def summarize(self, text: str, *, sentences: int = 3, task: str = "summarize") -> str:
        """Сжать текст до нескольких предложений."""
        if not text.strip():
            return ""
        brief = self._brief_line(text)
        if brief:
            sentences = 1
        prompt = f"Сожми до {sentences} предложений, сохранив факты и цифры:\n\n{text}"
        response = await self.complete(
            [Message.system(" ".join(part for part in (_SUMMARY_SYSTEM, brief) if part)),
             Message.user(prompt)],
            task=task,
            max_tokens=BRIEF_TOKENS if brief else None,
        )
        return response.text

    async def extract_intent(
        self,
        utterance: str,
        catalog: ToolCatalog,
        *,
        task: str = "intent",
        avoid: Sequence[str] = (),
        situation: str = "",
    ) -> ToolCall | None:
        """Определить, какой инструмент вызвать для реплики.

        Возвращает `None`, если модель не выбрала инструмент — тогда роутер
        передаст запрос дальше по цепочке.

        :param avoid: инструменты, которые для этой просьбы уже пробовали и
            которые владелец отверг. Модели о них говорится прямо: иначе она
            уверенно предложит то же самое, и отмена окажется бессмысленной.
        :param situation: что происходит прямо сейчас — время, режимы, открытый
            сайт (см. `core/situation.py`). Половина «он не понял» — это не
            непонятая фраза, а фраза, понятая вслепую.
        """
        schemas = catalog.function_schemas()
        if not schemas:
            return None

        question = utterance
        if avoid:
            question = (
                f"{utterance}\n\nДля этой просьбы уже пробовали и это оказалось не тем: "
                f"{', '.join(avoid)}. Выбери другой инструмент."
            )

        # Обстановка идёт в системное сообщение, а не в вопрос: вопрос — это то,
        # что сказал человек, и подмешивать туда служебное значило бы кормить
        # модель фразой, которой не звучало.
        system = _INTENT_SYSTEM
        if situation:
            system = f"{system}\n\nЧто происходит сейчас: {situation}"

        try:
            response = await self.complete(
                [Message.system(system), Message.user(question)],
                task=task,
                tools=schemas,
                tool_choice="auto",
            )
        except LLMError as exc:
            logger.warning("Разбор намерения через LLM не удался: %s", exc)
            return None

        return response.tool_calls[0] if response.has_tool_calls else None
