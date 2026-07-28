"""Telegram: отправка сообщений и чтение последних чатов.

Показывает связку двух каналов: событие о новом сообщении уходит в шину
(его может слушать кто угодно), а сжатие текста делается через LLM — но только
по запросу, а не на каждое сообщение.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from jarvis.core.contracts import Event, ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool


@dataclass(frozen=True, slots=True, kw_only=True)
class TelegramMessageReceived(Event):
    """Пришло новое сообщение в Telegram.

    Скилл объявляет собственное событие — ядро для этого править не нужно.
    """

    NAME: ClassVar[str] = "telegram.message.received"

    chat: str
    text: str


class TelegramSkill(Skill):
    """Чтение и отправка сообщений Telegram."""

    meta = SkillMeta(
        name="telegram",
        description="Сообщения и чаты Telegram",
        version="0.1.0",
    )

    async def on_setup(self) -> None:
        """Проверить токен бота."""
        self._token = str(self.context.setting("token", ""))
        if not self._token:
            self.log.warning("Токен Telegram не задан — инструменты вернут ошибку")

    @tool(phrases=["отправь сообщение {chat}"])
    async def send_message(self, chat: str, text: str) -> ToolResult:
        """Отправить сообщение в чат.

        :param chat: имя или идентификатор чата.
        :param text: текст сообщения.
        """
        if not self._token:
            return ToolResult.failure(
                "Нет токена Telegram",
                speech="Telegram не подключён: добавь токен бота в файл .env.",
            )
        # TODO: Bot API sendMessage
        self.log.info("Сообщение в %s: %s", chat, text)
        return ToolResult.success(
            {"chat": chat, "text": text},
            speech=f"Отправил сообщение в {chat}.",
        )

    @tool(phrases=["что нового в телеграме", "проверь телеграм", "новые сообщения"])
    async def get_recent_chats(self, limit: int = 5) -> ToolResult:
        """Вернуть последние непрочитанные чаты.

        :param limit: сколько чатов вернуть.
        """
        if not self._token:
            return ToolResult.failure(
                "Нет токена Telegram",
                speech="Telegram не подключён.",
            )
        # TODO: Bot API getUpdates
        chats: list[dict[str, str]] = []
        if not chats:
            return ToolResult.success([], speech="Новых сообщений нет.")
        return ToolResult.success(
            chats[:limit],
            speech=f"Непрочитанных чатов: {len(chats)}.",
        )

    @tool()
    async def summarize_chat(self, chat: str, limit: int = 50) -> ToolResult:
        """Пересказать переписку в чате.

        Показывает правильный порядок: сначала данные, и только потом LLM —
        и лишь тогда, когда без неё задачу не решить.

        :param chat: имя или идентификатор чата.
        :param limit: сколько последних сообщений брать.
        """
        # TODO: получить историю через Bot API
        history = ""
        if not history:
            return ToolResult.success("", speech=f"В чате {chat} нечего пересказывать.")

        summary = await self.context.llm.summarize(history)
        return ToolResult.success(summary, speech=summary)

    async def health(self) -> HealthStatus:
        """Готовность определяется наличием токена."""
        if not self._token:
            return HealthStatus.degraded("не задан токен бота")
        return HealthStatus.healthy("заглушка, токен задан")
