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

    @tool(phrases=["найди видео {query}", "поищи на ютубе {query}",
                   "find a video about {query}", "search youtube for {query}"])
    async def search_video(self, query: str, limit: int = 5) -> ToolResult:
        """Найти видео по запросу.

        :param query: поисковый запрос.
        :param limit: сколько результатов вернуть.
        """
        if not self._api_key:
            return ToolResult.failure(
                "Нет ключа YouTube API: задай JARVIS_YOUTUBE_KEY в .env",
                speech={"ru": "Поиск по Ютубу не настроен.", "en": "YouTube search isn't set up."},
            )
        # TODO: YouTube Data API v3, search.list
        results: list[dict[str, str]] = []
        if not results:
            return ToolResult.success(
                [],
                speech={"ru": f"По запросу «{query}» ничего не нашлось.",
                        "en": f"Nothing found for {query}."},
            )
        return ToolResult.success(
            results[:limit],
            speech={"ru": f"Нашёл {len(results)} видео. Первое: {results[0].get('title', '')}.",
                   "en": f"Found {len(results)} videos. First: {results[0].get('title', '')}."},
        )

    @tool(phrases=["включи видео {query}", "поставь {query} на ютубе",
                   "play {query} on youtube"])
    async def play_video(self, query: str) -> ToolResult:
        """Найти видео и запустить его на компьютере студии.

        Пример вызова чужого инструмента по имени: скилл не импортирует
        BrowserSkill, а просит реестр выполнить нужную команду. Собирать ссылку
        и открывать её самому не нужно — этим занимается браузерный скилл, и он
        же кодирует запрос.

        :param query: название или поисковый запрос.
        """
        # TODO: получить прямую ссылку на видео через search_video и открывать её
        if not self.tools.has("browser.search"):
            return ToolResult.failure(
                "открывать ссылки умеет скилл browser, а он не подключён",
                speech={"ru": "Не могу открыть браузер.", "en": "I can't open the browser."},
            )

        result = await self.tools.invoke(
            "browser.search", {"query": query, "engine": "youtube"}
        )
        if not result.ok:
            return result
        return ToolResult.success(
            result.value,
            speech={"ru": f"Включаю {query}.", "en": f"Playing {query}."},
        )

    @tool(phrases=["пауза", "останови видео", "pause", "stop the video"])
    async def pause(self) -> ToolResult:
        """Поставить воспроизведение на паузу."""
        # TODO: медиа-клавиши или API плеера
        return ToolResult.success(True, speech={"ru": "Пауза.", "en": "Paused."})

    async def health(self) -> HealthStatus:
        """Готовность определяется наличием ключа API."""
        if not self._api_key:
            return HealthStatus.degraded("не задан ключ API")
        return HealthStatus.healthy("заглушка, ключ задан")
