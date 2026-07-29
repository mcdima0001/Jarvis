"""Браузер: открыть сайт, поискать, закрыть окно.

Работает с любым браузером, потому что не работает ни с одним конкретно:
ссылка отдаётся системе, а та открывает её тем, что выбрано браузером по
умолчанию. Chrome, Firefox, Edge, Яндекс — разницы нет. Конкретный браузер
можно назвать в конфиге (``browser: chrome``), но это исключение, а не правило.

**Поиск делается ссылкой, а не набором текста на клавиатуре.** «Найди в гугле
котиков» превращается в ``google.com/search?q=котиков`` — результат тот же, что
от набора в строке поиска, но без единого нажатия. Эмуляция клавиатуры здесь
была бы худшим из возможных решений: текст ушёл бы в то окно, которое сейчас
в фокусе, а Jarvis запускают от администратора и в фокусе может оказаться
что угодно — от терминала до чужой формы. Голос — недоверенный ввод, и
превращать его в нажатия клавиш нельзя. Ссылка же не может ничего сделать,
кроме как открыться: запрос в неё попадает закодированным, а схема
проверяется — только ``http`` и ``https``, иначе ``steam://`` или ``file://``
из услышанного запустили бы совсем другое.

Закрытие окон — единственное место, где нужна операционная система. Скилл не
импортирует ``windows``, а просит реестр выполнить ``windows.close_program``:
нет этого инструмента — честно отказываемся.
"""

from __future__ import annotations

import asyncio
import re
import webbrowser
from urllib.parse import quote_plus, urlsplit

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool

#: Поисковые системы: куда подставить запрос.
ENGINES: dict[str, str] = {
    "google": "https://www.google.com/search?q={query}",
    "yandex": "https://yandex.ru/search/?text={query}",
    "duckduckgo": "https://duckduckgo.com/?q={query}",
    "youtube": "https://www.youtube.com/results?search_query={query}",
    "wikipedia": "https://ru.wikipedia.org/w/index.php?search={query}",
    "github": "https://github.com/search?q={query}",
    "maps": "https://yandex.ru/maps/?text={query}",
}

#: Как поисковики называют вслух. Слово сравнивается началом, поэтому падеж
#: значения не имеет: «в гугле», «в ютубе», «на яндексе» узнаются одинаково.
ENGINE_ALIASES: dict[str, str] = {
    "гугл": "google",
    "google": "google",
    "яндекс": "yandex",
    "yandex": "yandex",
    "дакдак": "duckduckgo",
    "duckduck": "duckduckgo",
    "ютуб": "youtube",
    "youtube": "youtube",
    "википеди": "wikipedia",
    "wikipedia": "wikipedia",
    "гитхаб": "github",
    "github": "github",
    "карт": "maps",
    "maps": "maps",
}

#: Сайты, которые зовут по-русски. Дополняется через ``sites`` в конфиге.
SITES: dict[str, str] = {
    "ютуб": "https://www.youtube.com",
    "гугл": "https://www.google.com",
    "яндекс": "https://ya.ru",
    "почта": "https://mail.google.com",
    "гитхаб": "https://github.com",
    "телеграм": "https://web.telegram.org",
    "твич": "https://www.twitch.tv",
    "стим": "https://store.steampowered.com",
    "википедия": "https://ru.wikipedia.org",
    "чат гпт": "https://chatgpt.com",
    "клод": "https://claude.ai",
    "реддит": "https://www.reddit.com",
    "карты": "https://yandex.ru/maps",
    "переводчик": "https://translate.google.com",
}

#: Браузеры: как называют вслух → имя процесса. Нужно только для закрытия.
BROWSERS: dict[str, tuple[str, ...]] = {
    "chrome.exe": ("хром", "гугл хром", "chrome", "google chrome"),
    "firefox.exe": ("фаерфокс", "файрфокс", "мозила", "firefox", "mozilla"),
    "msedge.exe": ("эдж", "едж", "edge", "microsoft edge"),
    "opera.exe": ("опера", "opera"),
    "browser.exe": ("яндекс браузер", "яндекс", "yandex browser"),
    "brave.exe": ("брейв", "brave"),
    "vivaldi.exe": ("вивальди", "vivaldi"),
}

#: Страница, которая открывается на голое «открой браузер».
DEFAULT_HOME = "https://www.google.com"

#: Схемы, которые разрешено открывать. Всё остальное — отказ: `os.startfile`
#: с чужой схемой запускает не браузер, а обработчик протокола.
SAFE_SCHEMES = frozenset({"http", "https"})

#: Голый домен: «зайди на example.com».
_DOMAIN = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(/[^\s]*)?$", re.IGNORECASE)

#: Короче четырёх букв сравнивать началом бессмысленно: «я» подойдёт к «яндекс».
_MIN_PREFIX = 4

#: Гласные на конце: по ним и различаются падежи — «почта», «почту», «почте».
_ENDINGS = "аеёиоуыэюяaeiouy"


def _stem(text: str) -> str:
    """Отбросить окончание, чтобы падеж перестал мешать сравнению."""
    return text.rstrip(_ENDINGS)


def safe_url(url: str) -> str | None:
    """Проверить ссылку перед открытием.

    :return: ссылку, если её можно отдать системе, иначе ``None``.
    """
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in SAFE_SCHEMES or not parts.netloc:
        return None
    return url.strip()


def site_url(name: str, sites: dict[str, str]) -> str | None:
    """Превратить услышанное в ссылку.

    Три случая: известное название («ютуб»), готовая ссылка и голый домен
    («example.com»). Всё остальное — не сайт.
    """
    text = " ".join(name.strip().lower().split())
    if not text:
        return None

    known = sites.get(text)
    if known:
        return safe_url(known)

    # Названия склоняют: «на ютубе», «в гитхабе», «открой почту».
    stem = _stem(text)
    if len(stem) >= _MIN_PREFIX:
        for spoken, url in sites.items():
            other = _stem(spoken)
            if len(other) >= _MIN_PREFIX and (
                stem.startswith(other) or other.startswith(stem)
            ):
                return safe_url(url)

    if "://" in text:
        return safe_url(text)
    if _DOMAIN.match(text):
        # Схему не подставляем молча к чему попало — только к тому, что
        # действительно похоже на домен.
        return safe_url(f"https://{text}")
    return None


def pick_engine(spoken: str, engines: dict[str, str], default: str) -> str:
    """Понять, в каком поисковике искать.

    Слово приходит из речи в любом падеже («в гугле», «на ютубе»), поэтому
    сравнивается началом. Незнакомое слово — не ошибка: «найди в интернете»
    означает «где обычно».
    """
    text = " ".join(spoken.strip().lower().split())
    if not text:
        return default
    if text in engines:
        return text
    for alias, engine in ENGINE_ALIASES.items():
        if text.startswith(alias) and engine in engines:
            return engine
    for name in engines:
        if len(name) >= _MIN_PREFIX and text.startswith(name):
            return name
    return default


def search_url(query: str, template: str) -> str | None:
    """Собрать ссылку на выдачу.

    Запрос кодируется целиком, поэтому ни пробелы, ни кавычки, ни амперсанд
    не могут выйти за пределы параметра.
    """
    text = query.strip()
    if not text:
        return None
    return safe_url(template.format(query=quote_plus(text)))


def browser_process(name: str) -> str | None:
    """Имя процесса браузера по тому, как его назвали."""
    text = " ".join(name.strip().lower().split())
    if not text:
        return None
    for image, spoken_names in BROWSERS.items():
        if any(text.startswith(spoken) or spoken.startswith(text) for spoken in spoken_names):
            return image
    return None


def running_browser(processes: list[str]) -> str | None:
    """Выбрать запущенный браузер из списка процессов."""
    lowered = {str(name).lower() for name in processes}
    for image in BROWSERS:
        if image in lowered:
            return image
    return None


class BrowserSkill(Skill):
    """Сайты, поиск и окна браузера."""

    meta = SkillMeta(
        name="browser",
        description="Работа с браузером: сайты, поиск, окна",
        version="0.1.0",
    )

    async def on_setup(self) -> None:
        """Прочитать настройки: домашняя страница, поисковик, свои сайты."""
        self._home = str(self.context.setting("home", DEFAULT_HOME))
        self._default_engine = str(self.context.setting("engine", "google"))
        self._browser = str(self.context.setting("browser", "") or "")

        self._sites = {
            **SITES,
            **{
                str(key).strip().lower(): str(value)
                for key, value in dict(self.context.setting("sites", {})).items()
            },
        }
        self._engines = dict(ENGINES)
        for key, value in dict(self.context.setting("engines", {})).items():
            template = str(value)
            if "{query}" not in template:
                self.log.warning(
                    "Поисковик %s пропущен: в шаблоне нет {query} — %s", key, template
                )
                continue
            self._engines[str(key).strip().lower()] = template

        if self._default_engine not in self._engines:
            self.log.warning(
                "Поисковик по умолчанию %r неизвестен, беру google. Есть: %s",
                self._default_engine,
                ", ".join(sorted(self._engines)),
            )
            self._default_engine = "google"

        self.log.info(
            "Браузер: %s, поиск через %s, сайтов в каталоге %d",
            self._browser or "системный по умолчанию",
            self._default_engine,
            len(self._sites),
        )

    # --- открытие ----------------------------------------------------------

    @tool(phrases=["открой браузер", "открой сайт {site}", "зайди на {site}",
                   "открой в браузере {site}",
                   "open the browser", "open site {site}", "go to {site}"])
    async def open_site(self, site: str = "") -> ToolResult:
        """Открыть сайт в браузере.

        :param site: название сайта («ютуб») или адрес; пусто — домашняя страница.
        """
        url = self._home if not site.strip() else site_url(site, self._sites)
        if url is None:
            return ToolResult.failure(
                f"не понял, какой сайт открывать: {site!r}",
                speech={
                    "ru": f"Не знаю такого сайта: {site}.",
                    "en": f"I don't know a site called {site}.",
                },
            )
        if not await self._open(url):
            return self._no_browser()

        name = site.strip() or "браузер"
        return ToolResult.success(
            {"url": url},
            speech={"ru": f"Открываю {name}.", "en": f"Opening {name}."},
        )

    @tool(phrases=["загугли {query}", "найди в {engine} {query}",
                   "найди на {engine} {query}", "поищи в {engine} {query}",
                   "покажи в браузере {query}",
                   "google {query}", "search {engine} for {query}"])
    async def search(self, query: str, engine: str = "") -> ToolResult:
        """Открыть поисковую выдачу по запросу.

        Ровно то же, что набрать запрос в строке поиска и нажать Enter, — но
        ссылкой, без нажатий клавиш.

        :param query: что искать.
        :param engine: где искать: гугл, яндекс, ютуб, википедия, гитхаб, карты.
        """
        chosen = pick_engine(engine, self._engines, self._default_engine)
        url = search_url(query, self._engines[chosen])
        if url is None:
            return ToolResult.failure(
                "пустой поисковый запрос",
                speech={"ru": "Не расслышал, что искать.", "en": "I didn't catch what to search for."},
            )
        if not await self._open(url):
            return self._no_browser()

        # Поисковик называем так, как назвал его владелец: своё «в гугле»
        # звучит лучше, чем внутреннее имя google, которое голос прочтёт
        # латиницей.
        where = engine.strip()
        speech = (
            {"ru": f"Ищу в {where}: {query}.", "en": f"Searching {where} for {query}."}
            if where
            else {"ru": f"Ищу: {query}.", "en": f"Searching for {query}."}
        )
        return ToolResult.success(
            {"url": url, "engine": chosen, "query": query}, speech=speech
        )

    # --- закрытие ----------------------------------------------------------

    @tool(phrases=["закрой браузер", "закрой окно браузера",
                   "close the browser", "close the browser window"])
    async def close(self, browser: str = "") -> ToolResult:
        """Закрыть окно браузера.

        :param browser: какой именно — «хром», «фаерфокс»; пусто — тот, что запущен.
        """
        if not self.tools.has("windows.close_program"):
            return ToolResult.failure(
                "закрывать окна умеет только скилл windows, а он не подключён",
                speech={
                    "ru": "Закрывать окна я тут не умею.",
                    "en": "I can't close windows on this machine.",
                },
            )

        image = browser_process(browser) if browser.strip() else await self._running()
        if image is None:
            return ToolResult.failure(
                f"не нашёл запущенного браузера ({browser!r})" if browser
                else "ни одного браузера не запущено",
                speech={
                    "ru": "Не вижу открытого браузера.",
                    "en": "I don't see a browser running.",
                },
            )

        # Своего кода закрытия здесь нет намеренно: окна умеет закрывать скилл
        # windows, и повторять его логику — значит чинить её потом дважды.
        return await self.tools.invoke("windows.close_program", {"program": image})

    async def _running(self) -> str | None:
        """Найти запущенный браузер через список процессов."""
        result = await self.tools.invoke("windows.list_programs", {})
        if not result.ok or not isinstance(result.value, list):
            return None
        return running_browser(result.value)

    # --- служебное ---------------------------------------------------------

    async def _open(self, url: str) -> bool:
        """Отдать ссылку браузеру. Открытие блокирующее — уводим в поток."""
        self.log.info("Открываю %s", url)
        try:
            return await asyncio.to_thread(self._open_blocking, url)
        except Exception as exc:  # noqa: BLE001 — сбой браузера не роняет скилл
            self.log.warning("Не удалось открыть %s: %s", url, exc)
            return False

    def _open_blocking(self, url: str) -> bool:
        """Открыть ссылку новым окном браузера."""
        if self._browser:
            try:
                return webbrowser.get(self._browser).open_new(url)
            except webbrowser.Error as exc:
                # Названный в конфиге браузер не найден — это не повод молчать,
                # но и не повод не открыть ссылку вовсе.
                self.log.warning(
                    "Браузер %r недоступен (%s) — открываю системным", self._browser, exc
                )
        return webbrowser.open_new(url)

    @staticmethod
    def _no_browser() -> ToolResult:
        """Ответ, когда система не смогла открыть ссылку."""
        return ToolResult.failure(
            "система не смогла открыть ссылку: браузер по умолчанию не найден",
            speech={
                "ru": "Не получилось открыть браузер.",
                "en": "Couldn't open the browser.",
            },
        )

    async def health(self) -> HealthStatus:
        """Готовность: есть ли в системе браузер по умолчанию."""
        try:
            await asyncio.to_thread(webbrowser.get)
        except webbrowser.Error as exc:
            return HealthStatus.degraded(f"браузер по умолчанию не найден: {exc}")
        return HealthStatus.healthy(f"поиск через {self._default_engine}")
