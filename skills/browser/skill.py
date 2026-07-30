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
import difflib
import json
import re
import secrets
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

from jarvis.core.contracts import ToolResult
from jarvis.core.net import WebSocketServer
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.text import romanize, skeleton, squash
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

#: Сайты и то, как их называют. Написания идут и русские, и латинские: Whisper
#: пишет английские названия латиницей ровно тогда, когда ему так послышалось,
#: и «зайди на YouTube» приходит в том же виде, что произнесено.
#: Дополняется через ``sites`` в конфиге.
SITES: dict[str, str] = {
    "ютуб": "https://www.youtube.com",
    "ютьюб": "https://www.youtube.com",
    "youtube": "https://www.youtube.com",
    "гугл": "https://www.google.com",
    "google": "https://www.google.com",
    "яндекс": "https://ya.ru",
    "yandex": "https://ya.ru",
    "яндекс музыка": "https://music.yandex.ru",
    "музыка": "https://music.yandex.ru",
    "yandex music": "https://music.yandex.ru",
    "яндекс диск": "https://disk.yandex.ru",
    "вконтакте": "https://vk.com",
    "кинопоиск": "https://www.kinopoisk.ru",
    "почта": "https://mail.google.com",
    "gmail": "https://mail.google.com",
    "гитхаб": "https://github.com",
    "github": "https://github.com",
    "телеграм": "https://web.telegram.org",
    "telegram": "https://web.telegram.org",
    "твич": "https://www.twitch.tv",
    "twitch": "https://www.twitch.tv",
    "стим": "https://store.steampowered.com",
    "steam": "https://store.steampowered.com",
    "википедия": "https://ru.wikipedia.org",
    "вики": "https://ru.wikipedia.org",
    "wikipedia": "https://ru.wikipedia.org",
    "чат гпт": "https://chatgpt.com",
    "чатгпт": "https://chatgpt.com",
    "chatgpt": "https://chatgpt.com",
    "клод": "https://claude.ai",
    "claude": "https://claude.ai",
    "реддит": "https://www.reddit.com",
    "reddit": "https://www.reddit.com",
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

#: Служебные страницы самого браузера. Открыть их можно только изнутри —
#: системе такую ссылку не отдашь, — поэтому они работают лишь с расширением.
#: У каждой несколько адресов: схема зависит от браузера (Яндекс понимает
#: browser://, Chrome — chrome://, Firefox — about:), и расширение пробует их
#: по очереди. Список закрытый: произвольную служебную ссылку из речи
#: открывать нельзя.
INTERNAL_PAGES: dict[str, tuple[str, ...]] = {
    "расширения": ("browser://extensions", "chrome://extensions", "about:addons"),
    "extensions": ("browser://extensions", "chrome://extensions", "about:addons"),
    "настройки браузера": (
        "browser://settings",
        "chrome://settings",
        "about:preferences",
    ),
    "settings": ("browser://settings", "chrome://settings", "about:preferences"),
    "история": ("browser://history", "chrome://history", "about:history"),
    "history": ("browser://history", "chrome://history", "about:history"),
    "загрузки": ("browser://downloads", "chrome://downloads", "about:downloads"),
    "downloads": ("browser://downloads", "chrome://downloads", "about:downloads"),
    "закладки": ("browser://bookmarks", "chrome://bookmarks", "about:bookmarks"),
    "bookmarks": ("browser://bookmarks", "chrome://bookmarks", "about:bookmarks"),
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

#: Кавычки, которые Whisper ставит вокруг названий: «открой вкладку «Marshall
#: Tech»» приходит именно так, вместе с ёлочками.
_QUOTES = " «»\"'`.,!?;:()[]"


def clean_spoken(text: str) -> str:
    """Убрать кавычки и лишние пробелы вокруг названия."""
    return " ".join(text.split()).strip(_QUOTES)


#: Костяк короче этого совпадёт со слишком многим: у «YouTube» он равен «tb».
_MIN_SKELETON = 5

#: Гласные на конце: по ним и различаются падежи — «почта», «почту», «почте».
_ENDINGS = "аеёиоуыэюяaeiouy"

#: Насколько похожим должно быть название, чтобы считаться тем же сайтом.
_SIMILARITY = 0.8


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
    text = clean_spoken(name).lower()
    if not text:
        return None

    known = sites.get(text)
    if known:
        return safe_url(known)

    # Названия склоняют: «на ютубе», «в гитхабе», «открой почту». И пишут
    # по-разному: сравниваем сжатые формы, где разделителей нет вовсе.
    stem = _stem(squash(text))
    if len(stem) >= _MIN_PREFIX:
        stems = {_stem(squash(spoken)): url for spoken, url in sites.items()}
        # Побеждает самое длинное подходящее название. «Яндекс музыку»
        # начинается с «яндекс», и без этого правила открывался поиск вместо
        # музыки — какая запись попадётся в словаре первой, такая и выигрывала.
        matches = [
            other
            for other in stems
            if len(other) >= _MIN_PREFIX
            and (stem.startswith(other) or other.startswith(stem))
        ]
        if matches:
            return safe_url(stems[max(matches, key=len)])

        # Whisper путает звонкие с глухими на конце: «гитхаб» слышится как
        # «гитхап», «твич» — как «твитч». Порог высокий: сайт открывается
        # молча, и промахнуться тут неприятнее, чем переспросить.
        close = difflib.get_close_matches(stem, list(stems), n=1, cutoff=_SIMILARITY)
        if close:
            return safe_url(stems[close[0]])

        # Название могли записать другим алфавитом, чем услышал Whisper:
        # «МаршалТех» в конфиге против «MarshallTech» в расшифровке. Согласный
        # костяк у обоих одинаковый; короткие костяки не берём — «tb» от
        # «YouTube» совпал бы со слишком многим.
        sounds = skeleton(text)
        if len(sounds) >= _MIN_SKELETON:
            for spoken, url in sites.items():
                if skeleton(spoken) == sounds:
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


#: Чем браузер подписывает своё имя в конце заголовка: «YouTube — Яндекс Браузер».
_TITLE_TAIL = re.compile(r"\s+[-–—]\s+")

#: Из заголовка берём слова целиком: «YouTube» в «Как удалить YouTube» — это
#: всё же про YouTube, а вот «youtube» внутри «myyoutube.ru» — нет.
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def page_title(title: str) -> str:
    """Отбросить имя браузера в конце заголовка окна.

    «YouTube — Яндекс Браузер» превращается в «YouTube». Без этого любое окно
    Яндекс Браузера считалось бы открытым Яндексом: имя браузера стоит в
    заголовке каждого окна.
    """
    parts = _TITLE_TAIL.split(title.strip())
    return parts[0].strip() if len(parts) > 1 else title.strip()


def site_keys(url: str, sites: dict[str, str], spoken: str = "") -> set[str]:
    """Слова, по которым сайт узнаётся в заголовке окна.

    Берутся все его написания из каталога — «ютуб», «ютьюб», «youtube», — плюс
    то, как его назвали. Домен не разбираем: у `mail.google.com` он дал бы
    «google», и почта нашлась бы в любом окне с гуглом.
    """
    keys = {name.strip().lower() for name, target in sites.items() if target == url}
    if spoken.strip():
        keys.add(spoken.strip().lower())
    if not keys:
        # Сайт назвали доменом: «зайди на example.com».
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        keys.add(host.split(".")[0])
    return {key for key in keys if len(key) >= 3}


def find_open_window(windows: list[dict], keys: set[str]) -> str | None:
    """Найти окно браузера, в котором этот сайт уже открыт.

    Видно только активную вкладку каждого окна — заголовок окна её и
    показывает. Вкладка, спрятанная в глубине чужого окна, снаружи браузера
    не видна никак, без расширения её не достать.

    :return: заголовок найденного окна либо ``None``.
    """
    for item in windows:
        if str(item.get("image", "")).lower() not in BROWSERS:
            continue
        title = str(item.get("title", ""))
        words = {word.lower() for word in _WORD.findall(page_title(title))}
        if keys & words:
            return title
    return None


def internal_page(name: str) -> tuple[str, ...]:
    """Служебная страница браузера по названию: «расширения», «история»."""
    text = clean_spoken(name).lower()
    if not text:
        return ()
    if text in INTERNAL_PAGES:
        return INTERNAL_PAGES[text]

    stem = _stem(text)
    if len(stem) < _MIN_PREFIX:
        return ()
    matches = [
        page
        for page in INTERNAL_PAGES
        if _stem(page).startswith(stem) or stem.startswith(_stem(page))
    ]
    return INTERNAL_PAGES[max(matches, key=len)] if matches else ()


#: Chrome помечает этим вкладку, которая не входит ни в одну группу.
NO_GROUP = -1


def by_proximity(tabs: list[dict], current: dict | None) -> list[dict]:
    """Расставить вкладки от ближних к дальним относительно текущей.

    У браузера бывают десятки вкладок с похожими заголовками, разложенных по
    группам, и «переключись на гитхаб» должно вести в тот гитхаб, рядом с
    которым сейчас работают. Признаков два:

    * **близость** — своя группа, своё окно, всё остальное. Группа живёт внутри
      окна, поэтому первое условие строже второго, а не рядом с ним;
    * **давность** — среди одинаково близких побеждает та вкладка, в которую
      смотрели позже. Название «МаршалТех» может носить и страница сервера, и
      таблица про него; из двух разных страниц с одним именем нужна почти
      всегда та, с которой недавно работали.
    """
    window = (current or {}).get("windowId")
    group = (current or {}).get("groupId", NO_GROUP)

    def rank(tab: dict) -> tuple[int, float]:
        if not current:
            distance = 0
        else:
            same_window = tab.get("windowId") == window
            if same_window and group != NO_GROUP and tab.get("groupId") == group:
                distance = 0
            else:
                distance = 1 if same_window else 2
        # Позже открытая — меньше по ключу, значит раньше в списке.
        return distance, -float(tab.get("lastAccessed") or 0)

    return sorted(tabs, key=rank)


def tabs_by_title(
    tabs: list[dict], spoken: str, current: dict | None = None
) -> list[int]:
    """Номера вкладок, чей заголовок похож на сказанное.

    Нужно для страниц, которых нет и не может быть в каталоге сайтов:
    настройки браузера, локальная разработка, открытый документ. «Закрой
    вкладку Extensions» иначе упиралось в «не знаю такого сайта».

    Сравнение идёт и по словам, и по сжатой форме: «MarshallTech» пишут слитно,
    а произносят раздельно, и наоборот.
    """
    wanted = clean_spoken(spoken).lower()
    if len(wanted) < 3:
        return []

    stem = _stem(wanted)
    tight = _stem(squash(wanted))
    # То же самое латиницей: «апи кей» произнесено по-русски, а на вкладке
    # написано «API Key». Костяк тут не поможет — он слишком короткий.
    # Окончание не отбрасываем: в латинской записи «y» и «e» на конце — часть
    # слова, а не падеж, и «apikey» превратилось бы в «apik».
    tight_latin = squash(romanize(wanted))
    # Название на странице может быть написано другим алфавитом, чем услышал
    # Whisper: «МаршалТех» на вкладке против «MarshallTech» в расшифровке.
    # Согласный костяк у обоих написаний одинаковый.
    sounds = skeleton(wanted)
    found: list[int] = []
    for tab in by_proximity(tabs, current):
        title = str(tab.get("title", "")).lower()
        page = page_title(title)
        words = {_stem(word) for word in _WORD.findall(page)}
        matched = wanted in title or (len(stem) >= _MIN_PREFIX and stem in words)
        # Сжатая форма ищется как кусок заголовка, поэтому порог выше: по
        # четырём буквам подряд совпадёт слишком многое.
        if not matched and len(tight) >= _MIN_PREFIX + 2:
            matched = tight in squash(page) or (
                len(tight_latin) >= _MIN_PREFIX + 2
                and tight_latin in squash(romanize(page))
            )
        if not matched and len(sounds) >= _MIN_SKELETON:
            matched = sounds in skeleton(page)
        if matched:
            identifier = tab.get("tabId")
            if isinstance(identifier, int):
                found.append(identifier)
    return found


def browser_process(name: str) -> str | None:
    """Имя процесса браузера по тому, как его назвали."""
    text = clean_spoken(name).lower()
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


#: Куда Jarvis кладёт токен для расширения и на каком порту его ждёт.
EXTENSION_DIR = "extension"
TOKEN_FILE = "token.json"
DEFAULT_PORT = 8765

#: Кому разрешено подключаться. Идентификатор расширения меняется при
#: переустановке, а схема — нет, поэтому сравнение по началу строки.
EXTENSION_ORIGINS = ("chrome-extension://", "moz-extension://")


def read_token(path: Path) -> str:
    """Прочитать сохранённый токен; пусто — если файла нет или он испорчен.

    Токен переживает перезапуск намеренно. Новый каждый раз означал бы, что
    расширение с прочитанным в память старым токеном перестаёт подключаться до
    перезагрузки браузера.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    token = data.get("token")
    return str(token) if isinstance(token, str) else ""


class _Extension:
    """Мост к расширению: команда туда, ответ обратно по тому же номеру.

    Соответствие вопроса и ответа держится на числовом ``id``: сообщений в
    сокете два потока, и без него ответ на «открой» можно было бы принять за
    ответ на «закрой».
    """

    def __init__(self, *, server: WebSocketServer, logger, timeout: float = 5.0) -> None:
        self._server = server
        self._log = logger
        self._timeout = timeout
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._last_id = 0
        self._last_error = ""
        #: Взводится, когда расширение поздоровалось. Нужен ожиданию ниже.
        self._ready = asyncio.Event()

    @property
    def connected(self) -> bool:
        """Подключено ли расширение прямо сейчас."""
        return self._server.connected

    async def ready(self, timeout: float = 0.0) -> bool:
        """Дождаться расширения, если оно ещё не подключилось.

        Служебный поток браузера просыпается по будильнику, и после запуска
        Jarvis проходит до полуминуты, прежде чем он выйдет на связь. Живой
        случай: `--say "включи трек …"` отработал за 0.18 с и получил
        «расширение не подключено», а расширение поздоровалось **в ту же
        секунду**, сразу после. Ждать тут дешевле, чем отказывать.

        Ждём только если ещё никто не приходил: обрыв посреди работы — дело
        обычное, и на нём вешать команду на секунды незачем.
        """
        if self._server.connected:
            return True
        if timeout <= 0 or self._ready.is_set():
            return False
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError:
            return False
        return self._server.connected

    @property
    def last_error(self) -> str:
        """Чем закончилась последняя неудачная команда.

        Отказ расширения — обычное дело («нет подходящей вкладки»), и звучать
        он должен своими словами, а не общим «не ответило».
        """
        return self._last_error

    async def call(self, action: str, **params: object) -> dict | None:
        """Выполнить команду в браузере.

        :return: поле ``result`` ответа, либо ``None``, если расширение не
            ответило или сообщило об ошибке. ``None`` — не исключение, а повод
            сделать по-старому: расширение необязательно.
        """
        if not self._server.connected:
            return None

        self._last_error = ""
        self._last_id += 1
        ident = self._last_id
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[ident] = future

        try:
            request = json.dumps({"id": ident, "action": action, "params": params})
            if not await self._server.send(request):
                return None
            reply = await asyncio.wait_for(future, self._timeout)
        except TimeoutError:
            self._log.warning("Расширение не ответило на %s за %.0f с", action, self._timeout)
            self._last_error = "расширение не ответило"
            return None
        finally:
            self._pending.pop(ident, None)

        if not reply.get("ok"):
            self._log.warning("Расширение отказало в %s: %s", action, reply.get("error"))
            self._last_error = str(reply.get("error") or "расширение отказало")
            return None
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    async def on_message(self, text: str) -> None:
        """Разобрать сообщение расширения: ответ на команду или событие."""
        try:
            message = json.loads(text)
        except ValueError:
            self._log.warning("Расширение прислало не JSON: %.80s", text)
            return
        if not isinstance(message, dict):
            return

        ident = message.get("id")
        if isinstance(ident, int):
            future = self._pending.get(ident)
            if future is not None and not future.done():
                future.set_result(message)
            return

        event = message.get("event")
        if event == "hello":
            self._ready.set()
            self._log.info("Расширение готово: %s", message.get("agent", ""))
        elif event and event != "keepalive":
            self._log.debug("Событие расширения: %s", event)


class BrowserSkill(Skill):
    """Сайты, поиск и окна браузера."""

    meta = SkillMeta(
        name="browser",
        description="Работа с браузером: сайты, поиск, окна",
        version="0.1.0",
    )

    async def on_setup(self) -> None:
        """Прочитать настройки: домашняя страница, поисковик, свои сайты."""
        self._server: WebSocketServer | None = None
        self._extension: _Extension | None = None
        #: Значение по умолчанию: моста может не быть вовсе, а читают его всегда.
        self._await_extension = 0.0
        self._home = str(self.context.setting("home", DEFAULT_HOME))
        self._default_engine = str(self.context.setting("engine", "google"))
        self._browser = str(self.context.setting("browser", "") or "")
        self._reuse = bool(self.context.setting("reuse", True))

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
        self._extension = self._prepare_extension()

    def _prepare_extension(self) -> _Extension | None:
        """Поднять мост к расширению и записать для него токен.

        Токен кладётся прямо в каталог расширения: страницам сайтов файлы
        расширения недоступны, поэтому передавать его руками не нужно. Пустой
        токен допустим — тогда остаётся только проверка origin.
        """
        settings = dict(self.context.setting("extension", {}))
        if not bool(settings.get("enabled", True)):
            self.log.info("Расширение отключено в конфиге — работаю окнами")
            return None

        port = int(settings.get("port", DEFAULT_PORT))
        directory = self.context.root / EXTENSION_DIR
        if not directory.is_dir():
            self.log.warning(
                "Каталог %s не найден — расширение подключать неоткуда", directory
            )
            return None

        path = directory / TOKEN_FILE
        token = read_token(path) or secrets.token_urlsafe(32)
        try:
            path.write_text(
                json.dumps({"token": token, "port": port}, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            self.log.warning("Не смог записать %s: %s", path, exc)
            return None

        origins = tuple(
            str(origin) for origin in settings.get("origins", EXTENSION_ORIGINS)
        )
        server = WebSocketServer(
            host=str(settings.get("host", "127.0.0.1")),
            port=port,
            token=token,
            origins=origins,
        )
        bridge = _Extension(
            server=server, logger=self.log, timeout=float(settings.get("timeout", 5.0))
        )
        # Сколько ждать самого первого подключения. Служебный поток браузера
        # просыпается по будильнику, и сразу после запуска Jarvis его ещё нет.
        self._await_extension = float(settings.get("await_ready", 5.0))
        server.on_message = bridge.on_message
        self._server = server
        return bridge

    async def on_start(self) -> None:
        """Начать слушать расширение."""
        if self._extension is not None and self._server is not None:
            await self._server.start()

    async def on_stop(self) -> None:
        """Закрыть порт."""
        if self._server is not None:
            await self._server.stop()

    # --- открытие ----------------------------------------------------------

    # «В браузере» на конце — способ сказать «именно сайт, а не программу»:
    # у GitHub и Telegram установлено и то, и другое, и «открой гитхаб» уходит
    # в windows.launch_program. Шаблон длиннее, поэтому проверяется раньше.
    @tool(phrases=["открой браузер", "открой сайт {site}", "зайди на {site}",
                   "открой в браузере {site}", "открой {site} в браузере",
                   "запусти {site} в браузере", "покажи {site} в браузере",
                   "открой вкладку {site}", "открою вкладку {site}",
                   "переключись на {site}", "переключи на {site}",
                   "покажи вкладку {site}",
                   # «Вкладка это вкладка»: без этих шаблонов слово уезжало в
                   # название сайта, и ассистент отвечал «не знаю такого сайта:
                   # вкладку Cloud CLI».
                   "переключись на вкладку {site}", "переключи на вкладку {site}",
                   "перейди на вкладку {site}", "перейди во вкладку {site}",
                   "open the browser", "open site {site}", "go to {site}",
                   "open {site} in the browser", "open the {site} tab",
                   "switch to {site}"])
    async def open_site(self, site: str = "") -> ToolResult:
        """Открыть сайт, служебную страницу браузера или открытую вкладку.

        :param site: название сайта («ютуб»), служебной страницы («расширения»),
            заголовок открытой вкладки или адрес; пусто — домашняя страница.
        """
        name = clean_spoken(site) or "браузер"
        spoken = clean_spoken(site)

        url = self._home if not spoken else site_url(spoken, self._sites)
        if url is None:
            # Не сайт — может быть, служебная страница браузера или уже
            # открытая вкладка. И то, и другое умеет только расширение.
            through_extension = await self._open_special(spoken, name)
            if through_extension is not None:
                return through_extension
            return ToolResult.failure(
                f"не понял, какой сайт открывать: {site!r}",
                speech={
                    "ru": f"Не знаю такого сайта: {name}.",
                    "en": f"I don't know a site called {name}.",
                },
            )

        reuse = self._reuse and bool(spoken)

        # С расширением всё делается вкладкой в уже открытом окне; без него
        # остаются окна и заголовки — путь хуже, но рабочий.
        through_tab = await self._open_tab(url, name, reuse=reuse)
        if through_tab is not None:
            return through_tab

        if reuse:
            opened = await self._focus_open(url, site)
            if opened is not None:
                return opened

        if not await self._open(url):
            return self._no_browser()

        return ToolResult.success(
            {"url": url},
            speech={
                "ru": (f"Открываю {name}.", f"{name} — открываю.", f"Секунду, {name}."),
                "en": (f"Opening {name}.", f"{name}, coming up."),
            },
        )

    # «За гугли» и «за гугл» — не опечатка: Whisper слышит «загугли» как два
    # слова и разбор уходил в платную модель. Дописать услышанное дешевле,
    # чем платить за каждую такую фразу.
    @tool(phrases=["загугли {query}", "за гугли {query}", "за гугл {query}",
                   "погугли {query}", "найди в {engine} {query}",
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
        # Вкладка того же сайта уже открыта — ведём её, а не плодим вторую.
        #
        # Раньше поиск всегда открывался новой вкладкой: «каждый запрос новый,
        # переиспользовать нечего». Для чужого сайта это верно — терять статью,
        # которую читают, из-за «загугли» нельзя. Но «открой видео X» на уже
        # открытом YouTube давало вторую вкладку YouTube, и владелец справедливо
        # назвал это поломкой. Поэтому условие — **тот же сайт**: выдача сменяет
        # выдачу, а посторонняя страница остаётся на месте.
        moved = await self._go(url)
        if moved is None:
            through_tab = await self._open_tab(url, query, reuse=False)
            if through_tab is None and not await self._open(url):
                return self._no_browser()

        # Поисковик называем так, как назвал его владелец: своё «в гугле»
        # звучит лучше, чем внутреннее имя google, которое голос прочтёт
        # латиницей.
        where = engine.strip()
        speech: dict[str, tuple[str, ...]] = (
            {
                "ru": (f"Ищу в {where}: {query}.", f"Смотрю в {where}: {query}."),
                "en": (f"Searching {where} for {query}.", f"Looking in {where}: {query}."),
            }
            if where
            else {
                "ru": (f"Ищу: {query}.", f"Смотрю: {query}.", f"Сейчас поищу: {query}."),
                "en": (f"Searching for {query}.", f"Looking up {query}.", f"Let's see: {query}."),
            }
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

    async def _open_special(self, spoken: str, name: str) -> ToolResult | None:
        """Служебная страница браузера или уже открытая вкладка по заголовку.

        Обе вещи снаружи браузера недоступны в принципе: `browser://extensions`
        системе не отдашь, а заголовки чужих вкладок ей неизвестны. Поэтому
        путь один — расширение; без него возвращаем ``None`` и отказываем.
        """
        if not spoken or self._extension is None or not self._extension.connected:
            return None

        pages = internal_page(spoken)
        if pages:
            result = await self._extension.call("open", urls=list(pages))
            if result is not None:
                self.log.info("Служебная страница: %s", result.get("url", pages[0]))
                return ToolResult.success(
                    result,
                    speech={
                        "ru": (f"Открываю {name}.", f"{name} — открываю."),
                        "en": (f"Opening {name}.", f"{name}, coming up."),
                    },
                )

        # Открытая вкладка: «переключись на Marshall Tech».
        listed = await self._extension.call("tabs") or {}
        open_tabs = listed.get("tabs", [])
        matching = tabs_by_title(open_tabs, spoken, listed.get("current"))
        if not matching:
            return None

        if len(matching) > 1:
            # Одно название носят разные страницы: сайт, таблица про него,
            # статья о нём. Выбор виден в логе — иначе разбирать такие случаи
            # приходится вслепую.
            titles = {tab.get("tabId"): tab.get("title", "") for tab in open_tabs}
            self.log.info(
                "Подошло вкладок: %d — %s",
                len(matching),
                "; ".join(repr(titles.get(item, "")) for item in matching[:4]),
            )

        result = await self._extension.call("activate", tabId=matching[0])
        if result is None:
            return None
        self.log.info("Переключаюсь на вкладку %r", result.get("title", name))
        return ToolResult.success(
            result,
            speech={
                "ru": (f"Показываю {name}.", f"Перехожу на {name}.", f"Вот {name}."),
                "en": (f"Switching to {name}.", f"Here's {name}.", f"Over to {name}."),
            },
        )

    async def _go(self, url: str) -> dict | None:
        """Увести открытую вкладку этого сайта по новому адресу.

        :return: ответ расширения, либо ``None``, если такой вкладки нет или
            расширение не подключено — тогда открываем новую, как раньше.
        """
        if self._extension is None or not self._extension.connected:
            return None
        moved = await self._extension.call("go", url=url, focus=True)
        if moved is not None:
            self.log.info("Веду открытую вкладку: %s", url)
        return moved

    async def _open_tab(self, url: str, name: str, *, reuse: bool) -> ToolResult | None:
        """Открыть адрес вкладкой через расширение.

        :return: готовый ответ, либо ``None``, если расширения нет или оно не
            ответило — тогда работаем окнами, как раньше.
        """
        if self._extension is not None:
            await self._extension.ready(self._await_extension)
        if self._extension is None or not self._extension.connected:
            return None

        result = await self._extension.call("open", url=url, reuse=reuse)
        if result is None:
            return None

        if result.get("reused"):
            self.log.info("Вкладка уже была открыта: %s", result.get("title", url))
            # Самая частая реплика ассистента за вечер: сайты переоткрывают
            # десятки раз. Одной строкой она успела надоесть владельцу первой.
            speech: dict[str, tuple[str, ...]] = {
                "ru": (
                    f"Переключаюсь на {name}.",
                    f"{name} уже открыт, показываю.",
                    f"Вот {name}.",
                    f"{name} — вот эта вкладка.",
                    f"Уже открыт, перехожу.",
                    f"Показываю {name}.",
                ),
                "en": (
                    f"Switching to {name}.",
                    f"{name} is already open, here it is.",
                    f"Here's {name}.",
                    f"Already open — over to it.",
                ),
            }
        else:
            speech = {
                "ru": (f"Открываю {name}.", f"{name} — открываю.", f"Секунду, {name}."),
                "en": (f"Opening {name}.", f"{name}, coming up."),
            }
        return ToolResult.success({"url": url, **result}, speech=speech)

    @tool(phrases=["закрой вкладку", "закрой вкладку {site}",
                   "close the tab", "close the {site} tab"])
    async def close_tab(self, site: str = "") -> ToolResult:
        """Закрыть вкладку с сайтом.

        :param site: название сайта; пусто — текущая вкладка.
        """
        if self._extension is None or not self._extension.connected:
            return ToolResult.failure(
                "закрывать вкладки умеет только расширение, а оно не подключено",
                speech={
                    "ru": "Расширение не подключено, вкладками не управляю.",
                    "en": "The extension isn't connected, so I can't manage tabs.",
                },
            )

        name = site.strip()
        if not name:
            tabs = await self._extension.call("tabs")
            active = next(
                (item for item in (tabs or {}).get("tabs", []) if item.get("active")), None
            )
            if active is None:
                return ToolResult.failure(
                    "не нашёл активную вкладку",
                    speech={"ru": "Не вижу открытых вкладок.", "en": "I see no open tabs."},
                )
            closed = await self._extension.call("close", tabId=active["tabId"])
            name = active.get("title", "вкладку")
        elif (url := site_url(name, self._sites)) is not None:
            closed = await self._extension.call("close", url=url)
        else:
            # Не всякая вкладка — известный сайт: настройки браузера, локальная
            # разработка, открытый документ. Ищем по заголовку.
            tabs = await self._extension.call("tabs") or {}
            matching = tabs_by_title(tabs.get("tabs", []), name, tabs.get("current"))
            if not matching:
                return ToolResult.failure(
                    f"вкладка {name!r} не найдена",
                    speech={
                        "ru": f"Не нашёл вкладку {name}.",
                        "en": f"No {name} tab found.",
                    },
                )
            closed = await self._extension.call("close", tabIds=matching)

        if not closed or not closed.get("closed"):
            return ToolResult.failure(
                f"вкладка {name!r} не найдена",
                speech={"ru": f"Не нашёл вкладку {name}.", "en": f"No {name} tab found."},
            )
        return ToolResult.success(
            closed,
            speech={
                "ru": (f"Закрываю {name}.", f"{name} закрыл.", f"Убрал {name}."),
                "en": (f"Closing {name}.", f"{name} closed.", f"Shut {name}."),
            },
        )

    # --- страница ----------------------------------------------------------

    # Оба инструмента служебные: голосом их не зовут, а место в каталоге,
    # который уезжает в модель на каждой неузнанной фразе, они бы занимали.
    # Разделение обязанностей тут такое: браузер знает про вкладки и адреса,
    # скилл `page` — про то, что делать внутри страницы.

    @tool(routable=False)
    async def page_target(self, site: str = "", active: bool = False) -> ToolResult:
        """Сказать, к какой вкладке относится команда о странице.

        :param site: название сайта; пусто — та вкладка, откуда идёт звук, а
            если тихо везде — та, в которой сейчас работают.
        :param active: брать вкладку в фокусе, даже если звук идёт из другой.
            «Нажми первую ссылку» относится к тому, куда смотрят, а не к тому,
            что играет в соседнем окне.
        """
        return await self._page_call("target", {"active": active}, site=site, tab=0)

    @tool(routable=False)
    async def page_run(self, plan: list[dict], site: str = "", tab: int = 0) -> ToolResult:
        """Выполнить действие внутри открытой страницы.

        :param plan: варианты-шаги; выполняется первый сработавший.
        :param site: в какой вкладке — название сайта; пусто — в той, что звучит.
        :param tab: номер вкладки, если он уже известен.
        """
        return await self._page_call("page", {"plan": list(plan)}, site=site, tab=tab)

    @tool(routable=False)
    async def page_go(self, url: str, tab: int = 0, focus: bool = False) -> ToolResult:
        """Увести вкладку по другому адресу и дождаться загрузки.

        Нужно скиллу `page`, чтобы открыть поиск **самого сайта**: нового окна
        при этом не появляется, работа продолжается в той же вкладке.

        Адрес сюда приходит не из речи, а собирается из шаблона сайта, и всё
        равно проверяется: `safe_url` пропускает только http и https, иначе
        услышанное `steam://` запустило бы обработчик протокола.

        :param url: полный адрес.
        :param tab: номер вкладки; пусто — вкладка того же сайта.
        :param focus: показать вкладку. Для поиска внутри страницы не нужно —
            там и так смотрят куда надо, а дёргать окно ассистент не должен.
        """
        address = safe_url(url)
        if address is None:
            return ToolResult.failure(f"недопустимый адрес: {url!r}")
        return await self._page_call(
            "go", {"url": address, "active": True, "focus": bool(focus)}, site="", tab=tab
        )

    @tool(routable=False)
    async def page_probe(self, site: str = "", tab: int = 0, limit: int = 40) -> ToolResult:
        """Перечислить кнопки открытой страницы.

        :param site: в какой вкладке — название сайта; пусто — в той, что звучит.
        :param tab: номер вкладки, если он уже известен.
        :param limit: сколько кнопок вернуть.
        """
        return await self._page_call("probe", {"limit": limit}, site=site, tab=tab)

    async def _page_call(
        self, action: str, params: dict, *, site: str, tab: int
    ) -> ToolResult:
        """Отправить расширению команду, работающую внутри страницы."""
        if self._extension is not None:
            await self._extension.ready(self._await_extension)
        if self._extension is None or not self._extension.connected:
            return ToolResult.failure(
                "работать со страницей умеет только расширение, а оно не подключено",
                speech={
                    "ru": "Расширение не подключено, со страницей не работаю.",
                    "en": "The extension isn't connected, so I can't touch the page.",
                },
            )

        target = dict(params)
        if tab:
            target["tabId"] = tab
        spoken = clean_spoken(site)
        if spoken:
            url = site_url(spoken, self._sites)
            if url is None:
                return ToolResult.failure(
                    f"не понял, о каком сайте речь: {site!r}",
                    speech={
                        "ru": f"Не знаю такого сайта: {spoken}.",
                        "en": f"I don't know a site called {spoken}.",
                    },
                )
            target["url"] = url

        result = await self._extension.call(action, **target)
        if result is None:
            reason = self._extension.last_error or "расширение не ответило"
            return ToolResult.failure(
                f"страница недоступна: {reason}",
                speech={
                    "ru": "Не нашёл подходящую вкладку.",
                    "en": "I couldn't find a suitable tab.",
                },
            )
        return ToolResult.success(result)

    async def _focus_open(self, url: str, spoken: str) -> ToolResult | None:
        """Переключиться на уже открытое окно с этим сайтом.

        :return: готовый ответ, если окно нашлось и поднялось; иначе ``None``,
            и сайт открывается как обычно.
        """
        if not self.tools.has("windows.list_windows"):
            return None

        listed = await self.tools.invoke("windows.list_windows", {})
        if not listed.ok or not isinstance(listed.value, list):
            return None

        title = find_open_window(listed.value, site_keys(url, self._sites, spoken))
        if title is None:
            return None

        self.log.info("Сайт уже открыт в окне %r — переключаюсь", title)
        raised = await self.tools.invoke("windows.focus_window", {"title": title})
        if not raised.ok:
            # Окно есть, но поднять его не вышло — лучше открыть новое, чем
            # отчитаться об успехе, которого пользователь не увидит.
            self.log.warning("Не удалось поднять окно %r, открою новое", title)
            return None

        name = spoken.strip()
        return ToolResult.success(
            {"url": url, "window": title, "reused": True},
            speech={
                "ru": f"{name} уже открыт, переключаюсь.",
                "en": f"{name} is already open, switching to it.",
            },
        )

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
        bridge = "расширение подключено" if (
            self._extension is not None and self._extension.connected
        ) else "без расширения, работаю окнами"
        try:
            await asyncio.to_thread(webbrowser.get)
        except webbrowser.Error as exc:
            return HealthStatus.degraded(f"браузер по умолчанию не найден: {exc}")
        return HealthStatus.healthy(f"поиск через {self._default_engine}, {bridge}")
