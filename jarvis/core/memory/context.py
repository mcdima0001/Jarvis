"""Сборка контекста для языковой модели.

Правило «никогда не отправлять всю память в LLM» здесь не соглашение, а
свойство API: метода «дай всё» просто нет — разделы называются явно, и результат
обрезается по бюджету токенов.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from .protocol import DocumentStore, JournalStore

logger = logging.getLogger(__name__)

#: Грубая оценка: один токен ≈ 4 символа кириллицы. Для бюджета этого хватает.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Оценить число токенов в тексте."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


class ContextBuilder:
    """Собирает контекст из явно названных разделов памяти."""

    def __init__(
        self,
        *,
        documents: DocumentStore,
        journals: JournalStore,
        budget_tokens: int = 2000,
    ) -> None:
        self._documents = documents
        self._journals = journals
        self._budget = budget_tokens

    @staticmethod
    def _render_document(name: str, data: Mapping[str, Any]) -> str:
        """Превратить документ в компактный текст."""
        if not data:
            return ""
        lines = [f"## {name}"]
        for key, value in sorted(data.items()):
            lines.append(f"- {key}: {value}")
        return "\n".join(lines)

    async def build(
        self,
        *,
        documents: Sequence[str] = (),
        journals: Sequence[str] = (),
        journal_limit: int = 10,
        budget_tokens: int | None = None,
    ) -> str:
        """Собрать контекст из перечисленных разделов.

        :param documents: имена документов, которые действительно нужны.
        :param journals: имена журналов.
        :param journal_limit: сколько последних записей брать из каждого журнала.
        :param budget_tokens: разовый бюджет вместо значения из конфига.
        """
        budget = budget_tokens if budget_tokens is not None else self._budget
        blocks: list[str] = []
        used = 0

        for name in documents:
            data = await self._documents.read(name)
            block = self._render_document(name, data)
            if not block:
                continue
            cost = estimate_tokens(block)
            if used + cost > budget:
                logger.debug("Раздел %s не помещается в бюджет контекста", name)
                continue
            blocks.append(block)
            used += cost

        for name in journals:
            entries = await self._journals.recent(name, limit=journal_limit)
            if not entries:
                continue
            lines = [f"## {name}"] + [f"- {entry.text}" for entry in entries]
            block = "\n".join(lines)
            cost = estimate_tokens(block)
            if used + cost > budget:
                logger.debug("Журнал %s не помещается в бюджет контекста", name)
                continue
            blocks.append(block)
            used += cost

        logger.debug("Контекст собран: %d раздел(ов), ~%d токен(ов)", len(blocks), used)
        return "\n\n".join(blocks)
