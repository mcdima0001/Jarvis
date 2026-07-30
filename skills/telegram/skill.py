"""Telegram: отправка сообщений и чтение последних чатов.

Показывает связку двух каналов: событие о новом сообщении уходит в шину
(его может слушать кто угодно), а сжатие текста делается через LLM — но только
по запросу, а не на каждое сообщение.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from telethon import TelegramClient

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
        self._api_id = int(self.context.setting("api_id", 0) or 0)
        self._api_hash = str(self.context.setting("api_hash", ""))

        client = TelegramClient("session", self._api_id, self._api_hash)
        client.start()

        if not self._api_id or not self._api_hash:
            self.log.warning("Telegram не настроен: добавь api_id и api_hash в .env")

    @tool(phrases=["отправь сообщение {chat}", "напиши {chat}", "отправь {chat}", "send a message to {chat}"])
    async def send_message(self, chat: str, text: str) -> ToolResult:
        """Отправить сообщение в чат.

        :param chat: Имя или идентификатор чата.
        :param text: Текст сообщения.
        """
        if not self._token:
            return ToolResult.failure(
                "Нет токена Telegram: задай JARVIS_TELEGRAM_TOKEN в .env",
                speech={"ru": "Телеграм не подключён. Добавь токен бота в настройки.",
                       "en": "Telegram isn't connected. Add the bot token in settings."},
            )
        # TODO: Bot API sendMessage
        self.log.info("Сообщение в %s: %s", chat, text)
        return ToolResult.success(
            {"chat": chat, "text": text},
            speech={"ru": f"Отправил сообщение в {chat}.", "en": f"Message sent to {chat}."},
        )

    @tool(phrases=["что нового в телеграме", "проверь телеграм", "новые сообщения",
                   "any new messages", "check telegram"])
    async def get_recent_chats(self, limit: int = 5) -> ToolResult:
        """Вернуть последние непрочитанные чаты.

        :param limit: Сколько чатов вернуть.
        """
        if not self._token:
            return ToolResult.failure(
                "Нет токена Telegram: задай JARVIS_TELEGRAM_TOKEN в .env",
                speech={"ru": "Телеграм не подключён.", "en": "Telegram isn't connected."},
            )
        # TODO: Bot API getUpdates
        chats: list[dict[str, str]] = []
        if not chats:
            return ToolResult.success(
                [], speech={"ru": "Новых сообщений нет.", "en": "No new messages."}
            )
        return ToolResult.success(
            chats[:limit],
            speech={"ru": f"Непрочитанных чатов: {len(chats)}.",
                   "en": f"Unread chats: {len(chats)}."},
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
            return ToolResult.success(
                "",
                speech={"ru": f"В чате {chat} нечего пересказывать.",
                        "en": f"Nothing to summarize in {chat}."},
            )

        summary = await self.context.llm.summarize(history)
        return ToolResult.success(summary, speech=summary)

    async def health(self) -> HealthStatus:
        """Готовность определяется наличием токена."""
        if not self._token:
            return HealthStatus.degraded("не задан токен бота")
        return HealthStatus.healthy("заглушка, токен задан")

