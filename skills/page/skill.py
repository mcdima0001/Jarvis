"""Страница: сделать во вкладке то, что обычно делают мышкой.

Скилл `browser` открывает и закрывает вкладки, этот — работает внутри них:
пауза, следующий трек, лайк, перемотка, нажатие названной кнопки. Разделение
не формальное: адреса и вкладки — дело браузера, а кнопки и плеер живут внутри
страницы и меняются вместе с ней.

**Обращение к модели тут — исключение, а не способ работы.** Порядок такой:

1. фраза узнаётся шаблоном и превращается в действие — бесплатно и мгновенно;
2. действие превращается в план: свои рецепты из конфига, выученное из памяти,
   встроенный рецепт сайта, общий способ. Всё это данные, а не запросы;
3. и только если ни один вариант не сработал, страница один раз описывается
   модели («вот кнопки, что нажать?»), а её выбор **уходит в память** — для
   этого сайта и этого действия вопрос больше не задаётся никогда.

Общий способ работает почти везде и без всяких рецептов, потому что опирается
не на разметку, а на две вещи, которые есть у всех: сам плеер (`<video>` и
`<audio>` умеют play/pause/громкость/перемотку из коробки) и подписи кнопок —
те самые, что читает экранный диктор. Разметку сайты переделывают каждый год,
а «Следующий трек» на кнопке остаётся.

Безопасность здесь та же, что и во всём проекте: **из голоса в страницу уходят
только данные**. Селектор попадает в `querySelector`, подпись — в сравнение
строк. Исполняемый код лежит в `extension/page.js`, приходит из расширения и
никогда — из речи или от модели. Набор шагов закрытый, и всё, что в него не
уложилось, отбрасывается ещё в Python (`validate_plan`), а потом ещё раз в
самой странице.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Mapping, Sequence
from urllib.parse import urlsplit

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.text import romanize
from jarvis.core.tools import tool

#: Что умеет сам плеер, без единой кнопки сайта.
MEDIA_ACTIONS = frozenset(
    {
        "play",
        "pause",
        "toggle",
        "mute",
        "unmute",
        "louder",
        "quieter",
        "forward",
        "back",
    }
)

#: Глаголы шага. Список закрытый: чего тут нет, того странице не отправить.
STEP_VERBS = ("media", "label", "click")

#: Сколько вариантов имеет смысл перебирать. Столько же режет и page.js.
MAX_STEPS = 8

#: Ограничения на строки внутри шага: это данные из памяти и от модели.
MAX_SELECTORS = 6
MAX_TEXT = 200

ACTION = Literal[
    "play",
    "pause",
    "toggle",
    "next",
    "previous",
    "like",
    "mute",
    "unmute",
    "louder",
    "quieter",
    "forward",
    "back",
]

#: Как выполнять действие на любом сайте.
#:
#: Порядок внутри — от надёжного к общему. Сначала плеер: он не зависит от
#: разметки вовсе. Потом подписи кнопок — они переживают редизайн, потому что
#: их читает экранный диктор, и менять их просто так никто не станет.
#:
#: Подписи сравниваются началом слова, а не любым куском: «like» внутри
#: «dislike» стоило бы дизлайка вместо лайка. Одного этого мало — по-русски
#: отрицание стоит **перед** словом («не нравится» содержит «нравится» целиком
#: и словом, и краем), поэтому у шага есть ещё и список запретных подписей.
ACTIONS: dict[str, tuple[dict[str, Any], ...]] = {
    "play": (
        {"media": "play"},
        {"label": ["воспроизвести", "слушать", "смотреть", "play", "включить"]},
    ),
    "pause": (
        {"media": "pause"},
        {"label": ["пауза", "приостановить", "pause"]},
    ),
    "toggle": ({"media": "toggle"},),
    "next": (
        {"label": ["следующий", "следующая", "следующее", "next track", "next video", "next song"]},
    ),
    "previous": (
        {"label": ["предыдущий", "предыдущая", "предыдущее", "previous"]},
    ),
    # Кнопок-близнецов тут три: лайк, дизлайк и «убрать лайк». Отличаются они
    # одним словом, и это слово — отрицание, поэтому запреты важнее совпадений.
    "like": (
        {
            "label": ["нравится", "лайк", "like"],
            "avoid": [
                "не нравится",
                "убрать",
                "отменить",
                "снять",
                "dislike",
                "remove",
                "undo",
            ],
        },
    ),
    "mute": ({"media": "mute"}, {"label": ["выключить звук", "mute", "без звука"]}),
    "unmute": ({"media": "unmute"}, {"label": ["включить звук", "unmute"]}),
    "louder": ({"media": "louder"},),
    "quieter": ({"media": "quieter"},),
    "forward": ({"media": "forward"},),
    "back": ({"media": "back"},),
}

#: Рецепты под конкретные сайты — на случай, когда подписи не хватает.
#:
#: Это подсказка, а не требование: селектор устаревает вместе с редизайном, и
#: тогда план просто идёт дальше, к общему способу. Поэтому неверный селектор
#: тут не ломает команду, а лишь перестаёт помогать.
SITE_RECIPES: dict[str, dict[str, tuple[dict[str, Any], ...]]] = {
    "youtube.com": {
        "next": ({"click": [".ytp-next-button"]},),
        "previous": ({"click": [".ytp-prev-button"]},),
        "like": (
            {"click": ["like-button-view-model button", "#segmented-like-button button"]},
        ),
    },
    "music.yandex.ru": {
        "next": ({"click": ['[data-test-id="NEXT_TRACK_BUTTON"]']},),
        "previous": ({"click": ['[data-test-id="PREV_TRACK_BUTTON"]']},),
        "play": ({"media": "play"}, {"click": ['[data-test-id="PLAY_BUTTON"]']}),
        "pause": ({"media": "pause"}, {"click": ['[data-test-id="PAUSE_BUTTON"]']}),
        "like": ({"click": ['[data-test-id="LIKE_BUTTON"]']},),
    },
    "vk.com": {
        "next": ({"click": [".audio_page_player_next"]},),
        "previous": ({"click": [".audio_page_player_prev"]},),
    },
}

#: Что сказать вслух. Реплики короткие: команда и так видна по результату.
SPEECH: dict[str, tuple[str, str]] = {
    "play": ("Включаю.", "Playing."),
    "pause": ("Пауза.", "Paused."),
    "toggle": ("Готово.", "Done."),
    "next": ("Следующий.", "Next one."),
    "previous": ("Предыдущий.", "Previous one."),
    "like": ("Лайкнул.", "Liked."),
    "mute": ("Заглушил вкладку.", "Tab muted."),
    "unmute": ("Вернул звук.", "Sound is back."),
    "louder": ("Громче.", "Louder."),
    "quieter": ("Тише.", "Quieter."),
    "forward": ("Перемотал вперёд.", "Skipped ahead."),
    "back": ("Перемотал назад.", "Skipped back."),
}

#: Чего именно мы хотим — этой строкой действие объясняется модели.
INTENT_TEXT: dict[str, str] = {
    "play": "включить воспроизведение",
    "pause": "поставить на паузу",
    "toggle": "переключить воспроизведение",
    "next": "включить следующий трек или видео",
    "previous": "вернуться к предыдущему треку или видео",
    "like": "поставить лайк текущему треку или видео",
    "mute": "выключить звук",
    "unmute": "включить звук",
    "louder": "сделать громче",
    "quieter": "сделать тише",
    "forward": "перемотать вперёд",
    "back": "перемотать назад",
}

_LEARN_SYSTEM = (
    "Ты выбираешь кнопку на веб-странице. Отвечай одним числом — номером кнопки "
    "из списка. Если подходящей кнопки нет, ответь 0. Больше ничего не пиши."
)

#: Первое целое число в ответе модели: она любит дописать «Кнопка 3».
_NUMBER = re.compile(r"-?\d+")


def host_of(url: str) -> str:
    """Домен адреса без ``www``: по нему ищется рецепт сайта."""
    try:
        host = urlsplit(str(url)).netloc.lower()
    except ValueError:
        return ""
    return host.split("@")[-1].split(":")[0].removeprefix("www.")


def recipes_for(host: str, catalog: Mapping[str, Any]) -> Mapping[str, Any]:
    """Рецепты, объявленные для этого домена.

    Домен сравнивается с хвоста: рецепт для ``youtube.com`` работает и на
    ``m.youtube.com``, и на ``music.youtube.com``, потому что это тот же сайт
    с той же разметкой.
    """
    if not host:
        return {}
    for name, actions in catalog.items():
        key = str(name).lower().removeprefix("www.")
        if host == key or host.endswith(f".{key}"):
            if isinstance(actions, Mapping):
                return actions
    return {}


def _strings(value: Any) -> list[str]:
    """Список непустых строк разумной длины — или пусто, если это не он."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    found = [
        str(item).strip()
        for item in value[:MAX_SELECTORS]
        if isinstance(item, (str, int, float)) and str(item).strip()
    ]
    return [item for item in found if len(item) <= MAX_TEXT]


def validate_plan(raw: Any) -> list[dict[str, Any]]:
    """Оставить от плана только то, что странице разрешено отправлять.

    Планы приходят из трёх мест, и двум из них верить нельзя: конфиг пишет
    человек, память лежит файлом на диске, а выбор кнопки делает языковая
    модель. Поэтому набор глаголов закрытый, строки ограничены по длине, а
    всё непонятое молча отбрасывается — тот же принцип, что и с названиями
    программ: выполняется только узнанное.
    """
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []

    plan: list[dict[str, Any]] = []
    for step in raw:
        if not isinstance(step, Mapping):
            continue
        verb = next((name for name in STEP_VERBS if name in step), "")
        if not verb:
            continue

        if verb == "media":
            value = str(step["media"]).strip().lower()
            if value not in MEDIA_ACTIONS:
                continue
            clean: dict[str, Any] = {"media": value}
            for extra in ("seconds", "amount"):
                number = step.get(extra)
                if isinstance(number, (int, float)) and not isinstance(number, bool):
                    clean[extra] = float(number)
        else:
            items = _strings(step[verb])
            if not items:
                continue
            clean = {verb: items}
            if verb == "label":
                # Запретные слова: подпись «не нравится» содержит «нравится»
                # целиком, и без них лайк оказывается дизлайком.
                avoid = _strings(step.get("avoid", ()))
                if avoid:
                    clean["avoid"] = avoid

        if clean not in plan:
            plan.append(clean)
        if len(plan) >= MAX_STEPS:
            break
    return plan


def merge_plans(*plans: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Склеить варианты по порядку, выбросив повторы."""
    merged: list[dict[str, Any]] = []
    for plan in plans:
        for step in plan or ():
            if step not in merged:
                merged.append(dict(step))
            if len(merged) >= MAX_STEPS:
                return merged
    return merged


def with_amount(plan: Sequence[Mapping[str, Any]], *, seconds: float, step: float) -> list[dict]:
    """Подставить в медиа-шаги, на сколько перематывать и насколько менять звук."""
    result: list[dict[str, Any]] = []
    for item in plan:
        clean = dict(item)
        if clean.get("media") in ("forward", "back") and seconds > 0:
            clean["seconds"] = float(seconds)
        if clean.get("media") in ("louder", "quieter") and step > 0:
            clean["amount"] = float(step)
        result.append(clean)
    return result


def label_variants(spoken: str) -> list[str]:
    """Как подпись кнопки может выглядеть на странице.

    Whisper пишет услышанное одним алфавитом, а на кнопке бывает другой,
    поэтому рядом с услышанным идёт его латинская запись. Перевод не
    подразумевается: «подписаться» и «subscribe» — разные слова, а не разные
    написания одного.
    """
    text = " ".join(str(spoken).split()).strip(" «»\"'`.,!?").lower()
    if not text:
        return []
    variants = [text]
    latin = romanize(text)
    if latin != text and latin not in variants:
        variants.append(latin)
    return variants


def choose_control(reply: str, controls: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Разобрать ответ модели: номер кнопки из показанного ей списка.

    Ответ числом выбран намеренно. Попроси модель вернуть селектор — получишь
    выдуманный: она честно допишет то, чего на странице нет. Номер же можно
    только либо назвать верно, либо промахнуться по списку, который у нас на
    руках.
    """
    match = _NUMBER.search(str(reply or ""))
    if not match:
        return None
    index = int(match.group())
    if index < 1 or index > len(controls):
        return None
    control = controls[index - 1]
    return dict(control) if isinstance(control, Mapping) else None


def control_plan(control: Mapping[str, Any]) -> list[dict[str, Any]]:
    """План из выбранной кнопки: сначала селектор, потом её подпись.

    Подпись здесь не украшение, а страховка: селектор устареет при ближайшем
    редизайне, а по подписи кнопка найдётся и после него.
    """
    steps: list[dict[str, Any]] = []
    selector = str(control.get("sel") or "").strip()
    if selector:
        steps.append({"click": [selector]})
    name = str(control.get("name") or "").strip().lower()
    if name:
        steps.append({"label": [name]})
    return validate_plan(steps)


def describe(controls: Sequence[Mapping[str, Any]], *, title: str, host: str, want: str) -> str:
    """Собрать вопрос к модели: страница, кнопки, чего мы хотим."""
    lines = [f"Страница: {title} ({host})", "Кнопки:"]
    for number, control in enumerate(controls, start=1):
        lines.append(f"{number}. {str(control.get('name', '')).strip()}")
    lines.append(f"Какую кнопку нажать, чтобы {want}?")
    return "\n".join(lines)


class PageSkill(Skill):
    """Управление содержимым открытой вкладки."""

    meta = SkillMeta(
        name="page",
        description="Управление тем, что открыто во вкладке: плеер, кнопки, лайки",
        version="0.1.0",
    )

    async def on_setup(self) -> None:
        """Прочитать настройки и свои рецепты."""
        self._learn = bool(self.context.setting("learn", True))
        self._task = str(self.context.setting("llm_task", "intent"))
        self._seconds = float(self.context.setting("seek_seconds", 15))
        self._volume_step = float(self.context.setting("volume_step", 0.1))
        self._section = str(self.context.setting("memory_section", "sites"))
        #: Выученное читается из памяти один раз и обновляется при записи.
        self._known: dict[str, Any] | None = None

        self._own: dict[str, dict[str, list[dict]]] = {}
        for site, actions in dict(self.context.setting("sites", {})).items():
            if not isinstance(actions, Mapping):
                self.log.warning("Рецепты сайта %s пропущены: ожидался список действий", site)
                continue
            cleaned = {
                str(name): validate_plan(plan)
                for name, plan in actions.items()
                if validate_plan(plan)
            }
            if cleaned:
                self._own[str(site).strip().lower()] = cleaned

        self.log.info(
            "Страницы: своих рецептов %d, встроенных %d, обучение %s",
            len(self._own),
            len(SITE_RECIPES),
            "включено" if self._learn else "выключено",
        )

    # --- команды -----------------------------------------------------------

    @tool()
    async def control(self, action: ACTION, site: str = "", seconds: float = 0) -> ToolResult:
        """Управлять тем, что открыто во вкладке: плеер, лайк, звук вкладки.

        :param action: что сделать: play, pause, toggle, next, previous, like,
            mute, unmute, louder, quieter, forward, back.
        :param site: на каком сайте — «ютуб», «яндекс музыка»; пусто — там,
            откуда сейчас идёт звук.
        :param seconds: на сколько секунд перематывать (forward и back).
        """
        return await self._act(str(action), site=site, seconds=seconds)

    @tool(phrases=["нажми {control}", "нажми кнопку {control}", "нажми на {control}",
                   "press {control}", "click {control}"])
    async def press(self, control: str, site: str = "") -> ToolResult:
        """Нажать кнопку на странице по её подписи.

        :param control: что написано на кнопке: «подписаться», «войти».
        :param site: на каком сайте; пусто — в той вкладке, где сейчас работаешь.
        """
        variants = label_variants(control)
        if not variants:
            return ToolResult.failure(
                "не расслышал, что нажимать",
                speech={"ru": "Не понял, что нажать.", "en": "I didn't catch what to press."},
            )
        name = variants[0]
        # Ключ памяти свой на каждую кнопку: «нажми подписаться» на этом сайте
        # выучивается один раз и дальше работает без модели, как и всё
        # остальное.
        return await self._act(
            f"press:{name}",
            site=site,
            extra=[{"label": variants}],
            want=f"нажать кнопку «{name}»",
            speech=(f"Нажал {name}.", f"Pressed {name}."),
        )

    # Обёртки под голос: фраза узнаётся мгновенно и бесплатно. В каталог для
    # модели они не идут — она и так всё это умеет через `control`, а каждый
    # инструмент уезжает в неё на каждой неузнанной фразе.
    @tool(routable=False, phrases=["пауза", "поставь на паузу", "останови видео",
                                   "останови музыку", "стоп",
                                   "поставь музыку на паузу", "поставь видео на паузу",
                                   "поставь {site} на паузу", "останови {site}",
                                   "pause", "stop the video", "pause {site}"])
    async def pause(self, site: str = "") -> ToolResult:
        """Поставить воспроизведение на паузу.

        :param site: где именно — «ютуб», «яндекс музыка»; пусто — там, откуда
            идёт звук.
        """
        return await self._act("pause", site=site, soft=True)

    @tool(routable=False, phrases=["сними с паузы", "включи воспроизведение",
                                   "продолжи воспроизведение", "продолжай играть",
                                   "сними музыку с паузы", "сними видео с паузы",
                                   "продолжи музыку", "продолжи видео",
                                   "сними {site} с паузы",
                                   "resume", "continue playing"])
    async def play(self, site: str = "") -> ToolResult:
        """Продолжить воспроизведение.

        :param site: где именно; пусто — там, где остановились.
        """
        return await self._act("play", site=site, soft=True)

    @tool(routable=False, phrases=["следующий трек", "следующее видео", "следующая песня",
                                   "переключи трек", "переключи песню", "дальше трек",
                                   "next track", "next video"])
    async def next_track(self) -> ToolResult:
        """Включить следующий трек или видео."""
        return await self._act("next")

    @tool(routable=False, phrases=["предыдущий трек", "предыдущее видео", "прошлый трек",
                                   "верни трек", "previous track", "previous video"])
    async def previous_track(self) -> ToolResult:
        """Вернуться к предыдущему треку или видео."""
        return await self._act("previous")

    @tool(routable=False, phrases=["лайкни", "поставь лайк", "мне нравится",
                                   "лайкни видео", "лайкни трек", "лайкни песню",
                                   "поставь лайк видео", "поставь лайк треку",
                                   "поставь лайк песне",
                                   "like this", "like it"])
    async def like(self) -> ToolResult:
        """Поставить лайк тому, что играет."""
        return await self._act("like")

    @tool(routable=False, phrases=["выключи звук во вкладке", "заглуши вкладку",
                                   "mute the tab"])
    async def mute_tab(self) -> ToolResult:
        """Выключить звук во вкладке, не трогая громкость системы."""
        return await self._act("mute")

    @tool(routable=False, phrases=["включи звук во вкладке", "верни звук во вкладке",
                                   "unmute the tab"])
    async def unmute_tab(self) -> ToolResult:
        """Вернуть звук во вкладке."""
        return await self._act("unmute")

    @tool(routable=False, phrases=["перемотай вперёд", "перемотай вперёд {seconds}",
                                   "перемотай на {seconds}", "промотай вперёд {seconds}",
                                   "skip ahead", "skip ahead {seconds}"])
    async def forward(self, seconds: float = 0) -> ToolResult:
        """Перемотать вперёд.

        :param seconds: на сколько секунд; пусто — на обычный шаг из конфига.
        """
        return await self._act("forward", seconds=seconds)

    @tool(routable=False, phrases=["перемотай назад", "перемотай назад {seconds}",
                                   "отмотай назад {seconds}", "верни назад {seconds}",
                                   "skip back", "skip back {seconds}"])
    async def back(self, seconds: float = 0) -> ToolResult:
        """Перемотать назад.

        :param seconds: на сколько секунд; пусто — на обычный шаг из конфига.
        """
        return await self._act("back", seconds=seconds)

    # --- работа ------------------------------------------------------------

    async def _act(
        self,
        action: str,
        *,
        site: str = "",
        seconds: float = 0,
        extra: Sequence[Mapping[str, Any]] = (),
        want: str = "",
        speech: tuple[str, str] | None = None,
        soft: bool = False,
    ) -> ToolResult:
        """Выполнить действие в подходящей вкладке.

        Порядок такой: найти вкладку → собрать план из известного → выполнить →
        и только при неудаче один раз спросить модель и запомнить ответ.

        :param soft: считать название сайта пожеланием, а не требованием.
            «Поставь ютуб на паузу» при закрытом ютубе разумно понять как
            «поставь на паузу»: остановить просят то, что звучит, и молчать
            из-за неудачно названного сайта тут хуже, чем выполнить. Для
            `press` и явного `control` название остаётся требованием: нажать
            кнопку не на том сайте — это уже не мелочь.
        """
        if not self.tools.has("browser.page_run"):
            return ToolResult.failure(
                "работать со страницей умеет расширение браузера, а скилл browser не подключён",
                speech={
                    "ru": "Со страницами я тут не работаю.",
                    "en": "I can't work with pages on this machine.",
                },
            )

        # Вкладку узнаём заранее: без неё неизвестен сайт, а значит и рецепт.
        # Это один обмен по локальному сокету, зато дальше всё однозначно.
        found = await self.tools.invoke("browser.page_target", {"site": site})
        if not found.ok and site and soft:
            self.log.info("Вкладки %r не нашлось — работаю с той, что звучит", site)
            found = await self.tools.invoke("browser.page_target", {})
        if not found.ok or not isinstance(found.value, Mapping):
            return found if not found.ok else self._nothing(action)
        target = dict(found.value)
        tab = int(target.get("tabId") or 0)
        host = host_of(str(target.get("url", "")))

        steps = self._plan(action, host, seconds=seconds, extra=extra)
        result = await self._run(steps, tab)
        if result is not None:
            return self._done(action, result, speech)

        learned = await self._learn_action(
            action, tab=tab, host=host, title=str(target.get("title", "")), want=want
        )
        if learned is not None:
            return self._done(action, learned, speech)
        return self._nothing(action, host=host)

    def _plan(
        self,
        action: str,
        host: str,
        *,
        seconds: float = 0,
        extra: Sequence[Mapping[str, Any]] = (),
    ) -> list[dict[str, Any]]:
        """Собрать варианты для действия на этом сайте.

        Порядок источников — от самого частного к самому общему: свой рецепт
        из конфига, выученное для этого сайта, встроенный рецепт, общий способ.
        Написанное человеком идёт первым: если он что-то поправил руками, это
        и есть ответ, а выученное когда-то могло уже устареть.
        """
        own = recipes_for(host, self._own).get(action, ())
        known = recipes_for(host, self._sites_memory()).get(action, ())
        built_in = recipes_for(host, SITE_RECIPES).get(action, ())
        common = ACTIONS.get(action, ())
        merged = merge_plans(
            validate_plan(own), validate_plan(known), built_in, validate_plan(extra), common
        )
        return with_amount(
            merged,
            seconds=seconds if seconds > 0 else self._seconds,
            step=self._volume_step,
        )

    async def _run(self, plan: Sequence[Mapping[str, Any]], tab: int) -> dict[str, Any] | None:
        """Отправить план в страницу; ``None`` — ни один вариант не сработал."""
        steps = validate_plan(plan)
        if not steps:
            return None
        result = await self.tools.invoke(
            "browser.page_run", {"plan": steps, "tab": tab}
        )
        if not result.ok or not isinstance(result.value, Mapping):
            return None
        return dict(result.value) if result.value.get("done") else None

    async def _learn_action(
        self, action: str, *, tab: int, host: str, title: str, want: str
    ) -> dict[str, Any] | None:
        """Спросить у модели, что нажать, и запомнить ответ навсегда.

        Это единственное место скилла, где тратятся токены, и оно устроено так,
        чтобы срабатывать один раз на сайт и действие: удачный выбор уходит в
        память и дальше берётся оттуда.
        """
        if not self._learn or not host:
            return None
        if not self.context.llm.available:
            self.log.debug("Модель не настроена — учиться не у кого")
            return None

        probed = await self.tools.invoke("browser.page_probe", {"tab": tab})
        controls = list((probed.value or {}).get("controls", [])) if probed.ok else []
        if not controls:
            return None

        question = describe(
            controls, title=title, host=host, want=want or INTENT_TEXT.get(action, action)
        )
        try:
            reply = await self.context.llm.ask(question, task=self._task, system=_LEARN_SYSTEM)
        except Exception as exc:  # noqa: BLE001 — без модели команда просто не выйдет
            self.log.warning("Не удалось спросить модель про кнопку: %s", exc)
            return None

        control = choose_control(reply, controls)
        if control is None:
            self.log.info("Модель не нашла кнопку для %s на %s (ответ %r)", action, host, reply)
            return None

        plan = control_plan(control)
        result = await self._run(plan, tab)
        if result is None:
            return None

        await self._remember(host, action, plan)
        self.log.info(
            "Запомнил: %s на %s — кнопка %r", action, host, control.get("name", "")
        )
        return result

    # --- память ------------------------------------------------------------

    def _sites_memory(self) -> dict[str, Any]:
        """Выученные рецепты по сайтам."""
        return {
            host: value.get("actions", {})
            for host, value in (self._known or {}).items()
            if isinstance(value, Mapping)
        }

    async def _remember(self, host: str, action: str, plan: Sequence[Mapping[str, Any]]) -> None:
        """Записать удачный способ в память — раздел ``sites``."""
        try:
            known = dict(await self.context.memory.documents.get(self._section, host, {}) or {})
            actions = dict(known.get("actions", {}))
            actions[action] = [dict(step) for step in plan]
            known["actions"] = actions
            await self.context.memory.documents.set(self._section, host, known)
        except Exception as exc:  # noqa: BLE001 — не записалось, но команда выполнена
            self.log.warning("Не смог запомнить способ для %s: %s", host, exc)
            return
        cache = dict(self._known or {})
        cache[host] = known
        self._known = cache

    async def on_start(self) -> None:
        """Поднять из памяти то, что уже выучено."""
        try:
            self._known = dict(await self.context.memory.documents.read(self._section))
        except Exception as exc:  # noqa: BLE001 — память необязательна для команд
            self.log.warning(
                "Раздел памяти %r недоступен (%s) — выученные способы не подхватятся. "
                "Проверь, что он объявлен в memory.documents",
                self._section,
                exc,
            )
            self._known = {}
        if self._known:
            self.log.info("Выученных сайтов в памяти: %d", len(self._known))

    # --- ответы ------------------------------------------------------------

    def _done(
        self, action: str, result: Mapping[str, Any], speech: tuple[str, str] | None
    ) -> ToolResult:
        """Успех: сказать коротко, подробности оставить в значении."""
        ru, en = speech or SPEECH.get(action, ("Готово.", "Done."))
        detail = str(result.get("detail", "")).strip()
        self.log.info(
            "Страница %s: %s (%s)", result.get("url", ""), action, detail or result.get("done")
        )
        return ToolResult.success(dict(result), speech={"ru": ru, "en": en})

    @staticmethod
    def _nothing(action: str, *, host: str = "") -> ToolResult:
        """Отказ, когда ни один способ не подошёл."""
        where = f" на {host}" if host else ""
        return ToolResult.failure(
            f"не нашёл, чем выполнить {action}{where}",
            speech={
                "ru": "Не нашёл, чем это сделать на странице.",
                "en": "I couldn't find a control for that on the page.",
            },
        )

    async def health(self) -> HealthStatus:
        """Готовность: есть ли мост к странице и что уже выучено."""
        if not self.tools.has("browser.page_run"):
            return HealthStatus.degraded("скилл browser не подключён — страницы недоступны")
        learned = sum(len(actions) for actions in self._sites_memory().values())
        return HealthStatus.healthy(
            f"рецептов своих {len(self._own)}, встроенных {len(SITE_RECIPES)}, "
            f"выученных {learned}"
        )
