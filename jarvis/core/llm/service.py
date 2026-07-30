"""LLMService — задачи поверх провайдеров.

Здесь живёт то, что не зависит от вендора: `ask`, `chat`, `summarize`,
`extract_intent`. Написано один раз и работает с любым провайдером, поэтому
добавление Gemini или Groq не тянет за собой копирование промптов.

Выбор модели идёт через профиль задачи (`dialog`, `code`, `summarize`,
`intent`), а сменить модель можно на лету: `llm.set_model("code", "...")`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from jarvis.core.errors import LLMError, LLMNotConfigured
from jarvis.core.tools import ToolCatalog

from .profiles import ProfileRegistry
from .protocol import LLMProvider, LLMRequest, LLMResponse, Message, ToolCall

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
    "просьба что-то сделать."
)

_SUMMARY_SYSTEM = (
    "Ты сжимаешь текст до сути. Пиши по-русски, без вступлений и оценок, "
    "только факты, которые важны для дальнейшей работы."
)


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
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.cost += float(usage.get("cost", 0.0) or 0.0)
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
    ) -> None:
        self._providers = dict(providers)
        self._profiles = profiles
        self._spending = Spending()

    @property
    def spending(self) -> Spending:
        """Расход токенов с момента запуска."""
        return self._spending

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
    ) -> LLMResponse:
        """Выполнить запрос по профилю задачи."""
        profile = self._profiles.get(task)
        provider = self._provider(profile.provider)

        payload = list(messages)
        if profile.system and not any(m.role == "system" for m in payload):
            payload.insert(0, Message.system(profile.system))

        request = LLMRequest(
            messages=payload,
            model=profile.model,
            temperature=profile.temperature,
            max_tokens=profile.max_tokens,
            tools=tools,
            tool_choice=tool_choice,
        )
        logger.debug("LLM запрос: задача=%s модель=%s", profile.task, profile.model)
        response = await provider.complete(request)

        self._spending.add(profile.task, response.usage)
        usage = response.usage
        logger.info(
            "LLM %s (%s): %s+%s токенов%s, всего за сеанс %s",
            profile.task,
            profile.model,
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
            f", ${float(usage['cost']):.5f}" if usage.get("cost") else "",
            self._spending.total_tokens,
        )
        return response

    # --- задачи ------------------------------------------------------------

    async def ask(
        self,
        prompt: str,
        *,
        task: str | None = None,
        system: str | None = None,
        context: str | None = None,
    ) -> str:
        """Задать одиночный вопрос и получить текстовый ответ.

        :param context: заранее собранный фрагмент памяти; полную память
            передавать нельзя — только нужные разделы (см. `ContextBuilder`).
        """
        messages: list[Message] = []
        if system:
            messages.append(Message.system(system))
        if context:
            messages.append(Message.system(f"Контекст:\n{context}"))
        messages.append(Message.user(prompt))
        response = await self.complete(messages, task=task)
        return response.text

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
        prompt = f"Сожми до {sentences} предложений, сохранив факты и цифры:\n\n{text}"
        response = await self.complete(
            [Message.system(_SUMMARY_SYSTEM), Message.user(prompt)],
            task=task,
        )
        return response.text

    async def extract_intent(
        self,
        utterance: str,
        catalog: ToolCatalog,
        *,
        task: str = "intent",
        avoid: Sequence[str] = (),
    ) -> ToolCall | None:
        """Определить, какой инструмент вызвать для реплики.

        Возвращает `None`, если модель не выбрала инструмент — тогда роутер
        передаст запрос дальше по цепочке.

        :param avoid: инструменты, которые для этой просьбы уже пробовали и
            которые владелец отверг. Модели о них говорится прямо: иначе она
            уверенно предложит то же самое, и отмена окажется бессмысленной.
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

        try:
            response = await self.complete(
                [Message.system(_INTENT_SYSTEM), Message.user(question)],
                task=task,
                tools=schemas,
                tool_choice="auto",
            )
        except LLMError as exc:
            logger.warning("Разбор намерения через LLM не удался: %s", exc)
            return None

        return response.tool_calls[0] if response.has_tool_calls else None
