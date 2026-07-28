"""Поиск в интернете с последующим сжатием результата.

Демонстрирует правильную последовательность: сначала фактические данные,
затем LLM — и только для того, что без неё не решается.
"""

from __future__ import annotations

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool


class SearchSkill(Skill):
    """Веб-поиск и краткие ответы по найденному."""

    meta = SkillMeta(
        name="search",
        description="Поиск информации в интернете",
        version="0.1.0",
    )

    async def on_setup(self) -> None:
        """Прочитать настройки поискового движка."""
        self._engine = str(self.context.setting("engine", "duckduckgo"))
        self._max_results = int(self.context.setting("max_results", 5))

    @tool(phrases=["найди {query}", "поищи {query}", "загугли {query}",
                   "search for {query}", "look up {query}"])
    async def web_search(self, query: str, limit: int = 5) -> ToolResult:
        """Найти страницы по запросу.

        :param query: поисковый запрос.
        :param limit: сколько результатов вернуть.
        """
        # TODO: запрос к поисковому движку
        results: list[dict[str, str]] = []
        if not results:
            return ToolResult.success(
                [],
                speech={"ru": f"Поиск пока не подключён, запрос был: {query}.",
                       "en": f"Search isn't connected yet. The query was: {query}."},
            )
        return ToolResult.success(
            results[: min(limit, self._max_results)],
            speech={"ru": f"Нашёл {len(results)} результатов.",
                   "en": f"Found {len(results)} results."},
        )

    @tool(phrases=["что такое {query}", "расскажи про {query}",
                   "what is {query}", "tell me about {query}"])
    async def answer(self, query: str) -> ToolResult:
        """Найти информацию и дать короткий ответ.

        :param query: тема вопроса.
        """
        found = await self.web_search(query)
        pages = found.value or []
        if not pages:
            return ToolResult.success(
                "",
                speech={"ru": f"Не нашёл ничего по запросу «{query}».",
                       "en": f"Found nothing for {query}."},
            )

        digest = "\n\n".join(f"{p.get('title', '')}\n{p.get('snippet', '')}" for p in pages)
        summary = await self.context.llm.summarize(digest, sentences=2)
        return ToolResult.success(summary, speech=summary)

    async def health(self) -> HealthStatus:
        """Скилл-заглушка всегда считается работоспособным."""
        return HealthStatus.healthy(f"заглушка, движок {self._engine}")
