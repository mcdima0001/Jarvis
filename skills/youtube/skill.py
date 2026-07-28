"""YouTube: поиск и воспроизведение роликов."""

from __future__ import annotations

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool


class YouTubeSkill(Skill):
    """Поиск видео и управление воспроизведением."""

    meta = SkillMeta(
        name="youtube",
        description="Поиск и воспроизведение видео",
        version="0.1.0",
    )

    async def on_setup(self) -> None:
        """Прочитать ключ API."""
        self._api_key = str(self.context.setting("api_key", ""))
        if not self._api_key:
            self.log.warning("Ключ YouTube не задан — поиск вернёт ошибку")

    @tool(phrases=["найди видео {query}", "поищи на ютубе {query}"])
    async def search_video(self, query: str, limit: int = 5) -> ToolResult:
        """Найти видео по запросу.

        :param query: поисковый запрос.
        :param limit: сколько результатов вернуть.
        """
        if not self._api_key:
            return ToolResult.failure(
                "Нет ключа YouTube API",
                speech="Поиск по YouTube не настроен.",
            )
        # TODO: YouTube Data API v3, search.list
        results: list[dict[str, str]] = []
        if not results:
            return ToolResult.success([], speech=f"По запросу «{query}» ничего не нашлось.")
        return ToolResult.success(
            results[:limit],
            speech=f"Нашёл {len(results)} видео. Первое: {results[0].get('title', '')}.",
        )

    @tool(phrases=["включи видео {query}", "поставь {query} на ютубе"])
    async def play_video(self, query: str) -> ToolResult:
        """Найти видео и запустить его на компьютере студии.

        Пример вызова чужого инструмента по имени: скилл не импортирует
        WindowsSkill, а просит реестр выполнить нужную команду.

        :param query: название или поисковый запрос.
        """
        # TODO: получить ссылку через search_video
        url = f"https://www.youtube.com/results?search_query={query}"

        if self.tools.has("windows.launch_program"):
            await self.tools.invoke("windows.launch_program", {"program": url})

        return ToolResult.success({"url": url}, speech=f"Включаю {query}.")

    @tool(phrases=["пауза", "останови видео"])
    async def pause(self) -> ToolResult:
        """Поставить воспроизведение на паузу."""
        # TODO: медиа-клавиши или API плеера
        return ToolResult.success(True, speech="Пауза.")

    async def health(self) -> HealthStatus:
        """Готовность определяется наличием ключа API."""
        if not self._api_key:
            return HealthStatus.degraded("не задан ключ API")
        return HealthStatus.healthy("заглушка, ключ задан")
