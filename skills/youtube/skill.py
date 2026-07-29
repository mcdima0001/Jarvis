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
                   "включи на ютубе {query}", "включи трейлер {query}",
                   "play {query} on youtube"])
    async def play_video(self, query: str) -> ToolResult:
        """Найти видео на YouTube и включить первое из найденного.

        Пример вызова чужих инструментов по имени: скилл не импортирует ни
        браузер, ни страницу, а просит реестр выполнить две команды подряд —
        открыть выдачу и нажать первый ролик. Ключ API для этого не нужен:
        выдачу открывает браузер, а нажимает расширение.

        :param query: название или поисковый запрос.
        """
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

        # Нажать первый ролик умеет скилл страницы, и только с расширением.
        # Не вышло — выдача всё равно открыта, поэтому это не провал команды, а
        # другой ответ: обещать «включаю», когда играть нечего, нельзя.
        played = (
            await self.tools.invoke("page.open_first", {})
            if self.tools.has("page.open_first")
            else None
        )
        if played is not None and played.ok:
            return ToolResult.success(
                {"query": query, **(played.value if isinstance(played.value, dict) else {})},
                speech={"ru": f"Включаю {query}.", "en": f"Playing {query}."},
            )

        self.log.info("Ролик сам не включился (%s) — оставляю выдачу открытой",
                      played.error if played is not None else "нет скилла page")
        return ToolResult.success(
            result.value,
            speech={
                "ru": f"Нашёл на Ютубе: {query}. Скажи «включи первое видео».",
                "en": f"Found {query} on YouTube. Say “play the first video”.",
            },
        )

    # Пауза жила здесь заглушкой: отвечала «Пауза.» и не делала ничего.
    # Теперь этим занимается скилл `page` — по-настоящему и на любом сайте,
    # а не только на YouTube. Две команды с одной фразой «пауза» спорили бы
    # между собой, поэтому осталась одна.

    async def health(self) -> HealthStatus:
        """Готовность определяется наличием ключа API."""
        if not self._api_key:
            return HealthStatus.degraded("не задан ключ API")
        return HealthStatus.healthy("заглушка, ключ задан")
