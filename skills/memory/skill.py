"""Голосовой доступ к памяти: запомнить, вспомнить, узнать о себе.

В отличие от остальных заглушек этот скилл работает по-настоящему — файловая
память уже реализована в ядре. Заодно он показывает разницу между двумя типами
хранилищ: журнал для фактов во времени и документ для устойчивых предпочтений.
"""

from __future__ import annotations

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool


class MemorySkill(Skill):
    """Работа с памятью ассистента голосом."""

    meta = SkillMeta(
        name="memory",
        description="Запоминание фактов и предпочтений",
        version="0.1.0",
    )

    async def on_setup(self) -> None:
        """Запомнить, какие разделы доступны."""
        self._journal = str(self.context.setting("journal", "today"))

    @tool(phrases=["запомни {text}", "запиши {text}", "remember {text}", "note that {text}"])
    async def remember(self, text: str, tag: str = "note") -> ToolResult:
        """Записать факт в журнал.

        :param text: что запомнить.
        :param tag: метка для последующей выборки.
        """
        entry = await self.context.memory.remember(text, journal=self._journal, tags=(tag,))
        return ToolResult.success(
            {"text": entry.text, "timestamp": entry.timestamp},
            speech={"ru": "Запомнил.", "en": "Noted."},
        )

    @tool(phrases=["что ты помнишь", "напомни что было", "что я просил",
                   "what do you remember", "what did i ask"])
    async def recall(self, limit: int = 5, tag: str = "") -> ToolResult:
        """Вспомнить последние записи.

        :param limit: сколько записей вернуть.
        :param tag: показать только записи с этой меткой.
        """
        entries = await self.context.memory.recall(
            journal=self._journal,
            limit=limit,
            tag=tag or None,
        )
        if not entries:
            return ToolResult.success(
                [],
                speech={"ru": "Пока ничего не записано.", "en": "Nothing recorded yet."},
            )

        texts = [entry.text for entry in entries]
        joined = "; ".join(texts)
        return ToolResult.success(
            texts,
            speech={"ru": f"Вот что помню: {joined}", "en": f"Here is what I remember: {joined}"},
        )

    @tool()
    async def set_preference(self, key: str, value: str) -> ToolResult:
        """Сохранить устойчивое предпочтение.

        Предпочтения живут в документе, а не в журнале: их правят, а не копят.

        :param key: название настройки.
        :param value: значение.
        """
        await self.context.memory.documents.set("preferences", key, value)
        return ToolResult.success({key: value}, speech={"ru": f"Записал: {key} — {value}.", "en": f"Saved: {key} is {value}."})

    @tool(phrases=["что ты знаешь обо мне", "what do you know about me"])
    async def about_me(self) -> ToolResult:
        """Показать профиль и предпочтения."""
        profile = await self.context.memory.documents.read("profile")
        preferences = await self.context.memory.documents.read("preferences")
        payload = {"profile": profile, "preferences": preferences}

        if not profile and not preferences:
            return ToolResult.success(
                payload,
                speech={"ru": "Пока я о тебе ничего не знаю.", "en": "I don't know anything about you yet."},
            )

        known = ", ".join(sorted({**profile, **preferences}))
        return ToolResult.success(
            payload,
            speech={"ru": f"Знаю про: {known}.", "en": f"I know about: {known}."},
        )

    async def health(self) -> HealthStatus:
        """Проверить, что журнал доступен для чтения."""
        try:
            await self.context.memory.recall(journal=self._journal, limit=1)
        except Exception as exc:
            return HealthStatus.degraded(f"память недоступна: {exc}")
        return HealthStatus.healthy()
