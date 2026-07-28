"""LLMService — задачи поверх провайдеров.

Здесь живёт то, что не зависит от вендора: `ask`, `chat`, `summarize`,
`extract_intent`. Написано один раз и работает с любым провайдером, поэтому
добавление Gemini или Groq не тянет за собой копирование промптов.

Выбор модели идёт через профиль задачи (`dialog`, `code`, `summarize`,
`intent`), а сменить модель можно на лету: `llm.set_model("code", "...")`.
"""

from __future__ import annotations

import logging
from typing import Mapping, Sequence

from jarvis.core.errors import LLMError, LLMNotConfigured
from jarvis.core.tools import ToolCatalog

from .profiles import ProfileRegistry
from .protocol import LLMProvider, LLMRequest, LLMResponse, Message, ToolCall

logger = logging.getLogger(__name__)

_INTENT_SYSTEM = (
    "Ты — маршрутизатор голосового ассистента, управляющего домашней студией. "
    "Определи, какой инструмент вызвать для реплики пользователя, и вызови его. "
    "Если ни один инструмент не подходит, ответь обычным текстом без вызова."
)

_SUMMARY_SYSTEM = (
    "Ты сжимаешь текст до сути. Пиши по-русски, без вступлений и оценок, "
    "только факты, которые важны для дальнейшей работы."
)


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
        return await provider.complete(request)

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
    ) -> ToolCall | None:
        """Определить, какой инструмент вызвать для реплики.

        Возвращает `None`, если модель не выбрала инструмент — тогда роутер
        передаст запрос дальше по цепочке.
        """
        schemas = catalog.function_schemas()
        if not schemas:
            return None

        try:
            response = await self.complete(
                [Message.system(_INTENT_SYSTEM), Message.user(utterance)],
                task=task,
                tools=schemas,
                tool_choice="auto",
            )
        except LLMError as exc:
            logger.warning("Разбор намерения через LLM не удался: %s", exc)
            return None

        return response.tool_calls[0] if response.has_tool_calls else None
