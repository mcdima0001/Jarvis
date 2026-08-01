"""Поиск в интернете с последующим сжатием результата.

Демонстрирует правильную последовательность: сначала фактические данные,
затем LLM — и только для того, что без неё не решается. Модель не знает, что
произошло после её обучения, поэтому пересказывать она должна найденное,
а не вспоминать.

Источников два, и они дополняют друг друга:

* **Википедия** — официальное API, ключа не требует, не блокирует и отвечает
  готовой выжимкой из первого абзаца. Закрывает «кто такой», «что такое»,
  «когда было» — то есть половину вопросов к ассистенту.
* **DuckDuckGo** — всё остальное: новости, цены, свежие события. Ключа тоже не
  просит, но выдачу приходится разбирать из HTML, и на частые запросы он
  отвечает страницей-заглушкой. Поэтому он второй, а не первый.

Порядок задаётся в конфиге (``skills.settings.search.engines``), так что
подключить платный поисковик с ключом — это добавить класс и строку в список.
"""

from __future__ import annotations

import html
import re
from typing import Protocol
from urllib.parse import quote

import httpx

from jarvis.core.contracts import ToolResult, detect_language
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool

#: Википедия требует User-Agent с контактами, иначе отвечает 403.
_USER_AGENT = "Jarvis/0.1 (https://github.com/mcdima0001/Jarvis)"

_DDG_LINK = re.compile(r'<a[^>]+class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_DDG_SNIPPET = re.compile(r'class="result-snippet"[^>]*>(.*?)</td>', re.S)
_TAGS = re.compile(r"<[^>]+>")

#: Сколько символов выдержки отдавать модели на пересказ. Первого абзаца
#: Википедии хватает на ответ в два-три предложения, а платим мы за каждый.
_DIGEST_CHARS = 700

#: Чем модель отвечает, когда ответа в выдержках нет. Метка нужна необычная:
#: обычное «нет» бывает и настоящим ответом («правда ли, что…» — «Нет, это
#: миф»), и такой ответ мы бы выбросили.
_NO_ANSWER = {"ru": "НЕТ ОТВЕТА", "en": "NO ANSWER"}


def _unanswered(summary: str) -> bool:
    """Сказала ли модель, что ответа в выдержках нет.

    Сравнение целиком, а не по началу: ответ, **начинающийся** с «нет», — это
    обычный ответ, а не отказ.
    """
    cleaned = summary.strip().strip(".!,:;\"'«»").upper()
    return cleaned in (_NO_ANSWER["ru"], _NO_ANSWER["en"])


def _plain(markup: str) -> str:
    """Выкинуть теги и восстановить HTML-мнемоники."""
    return " ".join(html.unescape(_TAGS.sub(" ", markup)).split())


class SearchProvider(Protocol):
    """Источник результатов поиска.

    Свой провайдер — это один метод. Ни скилл, ни ядро о нём ничего не знают.
    """

    @property
    def name(self) -> str:
        """Имя для конфига и логов."""
        ...

    async def search(
        self, client: httpx.AsyncClient, query: str, limit: int, language: str
    ) -> list[dict[str, str]]:
        """Найти страницы. Записи с ключами ``title``, ``snippet``, ``url``."""
        ...


class WikipediaProvider:
    """Википедия: официальное API, готовая выжимка, без блокировок."""

    @property
    def name(self) -> str:
        """Имя провайдера."""
        return "wikipedia"

    async def search(
        self, client: httpx.AsyncClient, query: str, limit: int, language: str
    ) -> list[dict[str, str]]:
        """Найти статьи и вернуть их первые абзацы."""
        host = f"https://{language}.wikipedia.org"
        found = await client.get(
            f"{host}/w/api.php",
            params={
                "action": "query",
                "format": "json",
                "list": "search",
                "srsearch": query,
                "srlimit": limit,
            },
        )
        found.raise_for_status()

        results: list[dict[str, str]] = []
        for item in found.json().get("query", {}).get("search", []):
            title = item["title"]
            summary = await client.get(
                f"{host}/api/rest_v1/page/summary/{quote(title.replace(' ', '_'))}"
            )
            if summary.status_code != 200:
                continue
            payload = summary.json()
            if not (extract := payload.get("extract", "")):
                continue
            results.append(
                {
                    "title": payload.get("title", title),
                    "snippet": extract,
                    "url": payload.get("content_urls", {})
                    .get("desktop", {})
                    .get("page", f"{host}/wiki/{quote(title)}"),
                }
            )
        return results


class DuckDuckGoProvider:
    """DuckDuckGo без ключа: лёгкая версия выдачи, разбираемая регулярками.

    Хрупкость осознанная. Ключа он не просит, но и гарантий не даёт: на частые
    запросы отвечает страницей без результатов, а разметку может поменять в
    любой день. Пустой список здесь — штатный исход, а не ошибка: скилл просто
    идёт к следующему источнику.
    """

    _URL = "https://lite.duckduckgo.com/lite/"

    @property
    def name(self) -> str:
        """Имя провайдера."""
        return "duckduckgo"

    async def search(
        self, client: httpx.AsyncClient, query: str, limit: int, language: str
    ) -> list[dict[str, str]]:
        """Разобрать выдачу лёгкой версии."""
        response = await client.post(
            self._URL,
            data={"q": query},
            headers={
                # Лёгкая версия отдаёт результаты только «браузеру».
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
                "Referer": self._URL,
            },
        )
        response.raise_for_status()
        page = response.text

        links = _DDG_LINK.findall(page)
        snippets = _DDG_SNIPPET.findall(page)
        return [
            {
                "title": _plain(title),
                "snippet": _plain(snippets[index]) if index < len(snippets) else "",
                "url": html.unescape(url),
            }
            for index, (url, title) in enumerate(links[:limit])
        ]


#: Готовые источники. Новый — это класс с одним методом плюс строка здесь.
PROVIDERS: dict[str, type[SearchProvider]] = {
    "wikipedia": WikipediaProvider,
    "duckduckgo": DuckDuckGoProvider,
}


class SearchSkill(Skill):
    """Веб-поиск и краткие ответы по найденному."""

    meta = SkillMeta(
        name="search",
        description="Поиск информации в интернете",
        version="0.2.0",
    )

    async def on_setup(self) -> None:
        """Собрать источники в порядке, заданном конфигом."""
        names = self.context.setting("engines", ["wikipedia", "duckduckgo"])
        self._max_results = int(self.context.setting("max_results", 3))
        self._timeout = float(self.context.setting("timeout", 15.0))
        self._client: httpx.AsyncClient | None = None

        self._providers: list[SearchProvider] = []
        for name in names:
            factory = PROVIDERS.get(str(name).lower())
            if factory is None:
                self.log.warning(
                    "Неизвестный поисковый движок %r — пропускаю. Есть: %s",
                    name,
                    ", ".join(sorted(PROVIDERS)),
                )
                continue
            self._providers.append(factory())

        self.log.info(
            "Поиск: %s", " → ".join(p.name for p in self._providers) or "(нет источников)"
        )

    async def on_stop(self) -> None:
        """Закрыть соединения."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        """Один клиент на весь скилл: соединения переиспользуются."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers={"User-Agent": _USER_AGENT},
            )
        return self._client

    async def _collect(self, query: str, limit: int, language: str) -> list[dict[str, str]]:
        """Опросить источники по очереди до первого непустого ответа."""
        for provider in self._providers:
            try:
                results = await provider.search(self._http(), query, limit, language)
            except Exception as exc:  # noqa: BLE001 — сбой источника не фатален
                self.log.warning("Источник %s не ответил: %s", provider.name, exc)
                continue
            if results:
                self.log.info("Источник %s: %d результатов", provider.name, len(results))
                return results
            self.log.info("Источник %s ничего не нашёл", provider.name)
        return []

    # «Загугли» отдано скиллу browser: по смыслу это «открой мне гугл», а не
    # «найди и расскажи». Здесь остаются формулировки, на которые ждут ответа
    # голосом, а не открытой вкладки.
    @tool(phrases=["найди {query}", "поищи {query}",
                   "search for {query}", "look up {query}"])
    async def web_search(self, query: str, limit: int = 3) -> ToolResult:
        """Найти страницы по запросу.

        :param query: поисковый запрос.
        :param limit: сколько результатов вернуть.
        """
        language = detect_language(query, default="ru")
        results = await self._collect(query, min(limit, self._max_results), language)
        if not results:
            return ToolResult.success(
                [],
                speech={
                    "ru": f"Ничего не нашёл по запросу {query}.",
                    "en": f"Found nothing for {query}.",
                },
            )
        return ToolResult.success(
            results,
            speech={
                "ru": f"Нашёл {len(results)}. Первый: {results[0]['title']}.",
                "en": f"Found {len(results)}. First one: {results[0]['title']}.",
            },
        )

    async def _ask_source(
        self, provider: SearchProvider, query: str, language: str
    ) -> str | None:
        """Спросить один источник и попробовать собрать по нему ответ.

        :return: ответ; ``None`` — этот источник вопрос не закрывает, надо
            идти к следующему.
        """
        try:
            pages = await provider.search(self._http(), query, self._max_results, language)
        except Exception as exc:  # noqa: BLE001 — сбой источника не фатален
            self.log.warning("Источник %s не ответил: %s", provider.name, exc)
            return None
        if not pages:
            self.log.info("Источник %s ничего не нашёл", provider.name)
            return None
        self.log.info("Источник %s: %d результатов", provider.name, len(pages))

        # Выдержки обрезаются: ответ всё равно нужен в два-три предложения, а
        # каждый лишний абзац — это входные токены в каждом таком вопросе.
        digest = "\n\n".join(
            f"{page['title']}\n{page['snippet'][:_DIGEST_CHARS]}"
            for page in pages
            if page["snippet"]
        )
        if not digest:
            return None
        if not self.context.llm.available:
            # Без модели отдаём первый абзац как есть — это уже связный текст.
            return pages[0]["snippet"]

        instruction = (
            f"Ответь на вопрос «{query}» по этим выдержкам. Два-три предложения, "
            f"без вступлений и списков, только суть. Ответ будет прочитан вслух. "
            f"Если ответа на вопрос в выдержках нет, ответь ровно: {_NO_ANSWER['ru']}"
            if language == "ru"
            else f"Answer the question '{query}' from these excerpts. Two or three "
            f"sentences, no preamble or lists. The answer will be read aloud. "
            f"If the excerpts do not contain the answer, reply exactly: {_NO_ANSWER['en']}"
        )
        summary = await self.context.llm.ask(f"{instruction}\n\n{digest}", task="summarize")
        if _unanswered(summary):
            self.log.info(
                "Источник %s нашёл страницы, но ответа на %r в них нет — иду дальше",
                provider.name,
                query,
            )
            return None
        return summary

    @tool(phrases=["что такое {query}", "расскажи про {query}", "кто такой {query}",
                   "what is {query}", "tell me about {query}", "who is {query}"])
    async def answer(self, query: str) -> ToolResult:
        """Найти информацию и дать короткий ответ своими словами.

        Источники перебираются **по ответу, а не по наличию страниц**. Это не
        придирка: «как варить борщ» Википедия находит прекрасно — статьёй о
        самом супе, — и на этом перебор раньше заканчивался, а вслух звучало
        «в выдержках нет информации о том, как варить борщ». Страницы были,
        ответа не было, а DuckDuckGo с рецептом рядом даже не спросили.

        :param query: тема вопроса.
        """
        language = detect_language(query, default="ru")
        for provider in self._providers:
            found = await self._ask_source(provider, query, language)
            if found:
                return ToolResult.success(found, speech=found)

        return ToolResult.success(
            "",
            speech={
                "ru": f"Не нашёл ответа на вопрос {query}.",
                "en": f"I found no answer for {query}.",
            },
        )

    async def health(self) -> HealthStatus:
        """Скилл работоспособен, пока есть хотя бы один источник."""
        if not self._providers:
            return HealthStatus.degraded("не настроен ни один поисковый движок")
        return HealthStatus.healthy(" → ".join(p.name for p in self._providers))
