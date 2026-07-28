"""Память: документы, журналы и сборка контекста."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from jarvis.core.config import MemoryConfig

from .context import ContextBuilder, estimate_tokens
from .document_store import FileDocumentStore
from .journal_store import FileJournalStore
from .protocol import DocumentStore, JournalEntry, JournalStore


class Memory:
    """Фасад памяти: документы, журналы и сборщик контекста в одном объекте."""

    def __init__(
        self,
        *,
        documents: DocumentStore,
        journals: JournalStore,
        context: ContextBuilder,
    ) -> None:
        self.documents = documents
        self.journals = journals
        self.context = context

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "memory"

    async def start(self) -> None:
        """Каталоги создаются в конструкторах хранилищ — поднимать нечего."""

    async def stop(self) -> None:
        """Файловые хранилища не держат открытых ресурсов."""

    # --- частые операции ---------------------------------------------------

    async def remember(
        self,
        text: str,
        *,
        journal: str = "today",
        tags: Sequence[str] = (),
        data: Mapping[str, Any] | None = None,
    ) -> JournalEntry:
        """Записать факт в журнал."""
        return await self.journals.append(journal, text, tags=tags, data=data)

    async def recall(
        self,
        *,
        journal: str = "today",
        limit: int = 10,
        tag: str | None = None,
    ) -> list[JournalEntry]:
        """Вспомнить последние записи журнала."""
        return await self.journals.recent(journal, limit=limit, tag=tag)


def build_memory(config: MemoryConfig) -> Memory:
    """Собрать память по конфигурации."""
    documents = FileDocumentStore(config.dir / "documents", config.documents)
    journals = FileJournalStore(config.dir / "journals", config.journals)
    context = ContextBuilder(
        documents=documents,
        journals=journals,
        budget_tokens=config.context_budget_tokens,
    )
    return Memory(documents=documents, journals=journals, context=context)


__all__ = [
    "ContextBuilder",
    "DocumentStore",
    "FileDocumentStore",
    "FileJournalStore",
    "JournalEntry",
    "JournalStore",
    "Memory",
    "build_memory",
    "estimate_tokens",
]
