"""Голосовой доступ к памяти: запомнить, вспомнить, узнать о себе.

В отличие от остальных заглушек этот скилл работает по-настоящему — файловая
память уже реализована в ядре. Заодно он показывает разницу между двумя типами
хранилищ: журнал для фактов во времени и документ для устойчивых предпочтений.

Поверх ручных инструментов работает фоновый автосбор профиля: раз в несколько
минут скилл просматривает только новые записи журнала и вытаскивает из них
простые факты о пользователе. Разбор идёт регулярными выражениями, без единого
обращения к языковой модели, поэтому фон не тратит токены.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import Any

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool
from jarvis.core.tts.normalize import plural_form

#: Как ключи профиля звучат вслух: «Знаю про: city» произнести нельзя.
_SPOKEN_KEYS = {
    "name": "имя",
    "city": "город",
    "job": "работа",
    "birthday": "день рождения",
    "likes": "что нравится",
    "dislikes": "что не нравится",
}

#: Сколько записей журнала зачитывать вслух; остальное — только в данных ответа.
_SPOKEN_RECORDS = 2


def _spoken_key(key: str) -> str:
    """Ключ профиля словами; неизвестный — с пробелами вместо подчёркиваний."""
    return _SPOKEN_KEYS.get(key, key.replace("_", " "))


def _memory_failure(exc: Exception) -> ToolResult:
    return ToolResult.failure(
        f"память недоступна: {exc}",
        speech={"ru": "Не могу добраться до памяти.", "en": "I can't reach my memory."},
    )


#: Сколько слов берём в значение: «я люблю гулять по вечерам с собакой в парке»
#: — это уже рассказ, а не предпочтение, и в профиль он не нужен целиком.
_MAX_WORDS = 4

#: Слова, с которых не начинается ни имя, ни предпочтение: «я люблю тебя»,
#: «мне нравится это», «я из дома» — фразы, а не факты о владельце.
_NOT_A_FACT = frozenset({
    "тебя", "вас", "его", "её", "ее", "их", "это", "этого", "этот", "эту", "то", "так", "когда",
    "что", "как", "всё", "все", "себя", "дома", "дом", "работы", "магазина", "школы", "туалета",
    "you", "it", "this", "that", "them", "him", "her", "home", "work",
})

#: Как зовут месяцы: день рождения без числа и без месяца — не дата.
_MONTHS = ("январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август", "сентябр",
           "октябр", "ноябр", "декабр", "jan", "feb", "mar", "apr", "may", "jun", "jul",
           "aug", "sep", "oct", "nov", "dec")


def _plausible(key: str, value: str) -> str:
    """Оставить от найденного только правдоподобный факт; пусто — не факт.

    Первая версия автопамяти брала всё после «я из» и «я люблю»: «я из дома»
    записывалось городом «дома», «я люблю тебя» — предпочтением «тебя» (разбор
    владельца 14.09.2026). Правила простые и по одному на ключ.
    """
    words = value.split()
    if not words or words[0].lower() in _NOT_A_FACT:
        return ""
    if key in ("name", "city"):
        # Имя и город распознавание пишет с большой буквы, а «дома», «работы» —
        # с маленькой. Берём подряд идущие слова с большой: «Нижний Новгород».
        proper = []
        for word in words:
            if not word[:1].isupper():
                break
            proper.append(word)
        return " ".join(proper[:3])
    if key == "job":
        # «Я работаю программистом», «я работаю в Яндексе» — факт; «я работаю над
        # проектом», «я работаю сегодня» — нет.
        first = words[0].lower()
        if first in ("в", "на", "at") and len(words) > 1 and words[1][:1].isupper():
            return " ".join(words[:_MAX_WORDS])
        if re.search(r"(ом|ем|ём|ой|ей|ью)$", first) and first not in ("над", "сегодня", "дома"):
            return " ".join(words[:_MAX_WORDS])
        return value if re.match(r"(?i)^(as|an?)\s", value) else ""
    if key == "birthday":
        lowered = value.lower()
        if not re.search(r"\d", lowered) and not any(month in lowered for month in _MONTHS):
            return ""
    return " ".join(words[:_MAX_WORDS])


# Максимальная длина сохраняемого значения: профиль должен оставаться коротким,
# иначе его чтение начнёт стоить дорого при каждом запросе.
_VALUE_LIMIT = 120

# Шаблоны разбора: один шаблон — одна группа со значением и один ключ профиля.
# Отрицательные формы идут раньше утвердительных, чтобы «я не люблю» не попало
# в ключ likes.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?:меня зовут|мо[ёе] имя|my name is|call me)\s+([^,.;!?]{2,40})",
            re.IGNORECASE,
        ),
        "name",
    ),
    (
        re.compile(
            r"(?:я живу в|я из|i live in|i'?m from)\s+([^,.;!?]{2,40})",
            re.IGNORECASE,
        ),
        "city",
    ),
    (
        re.compile(
            r"(?:я работаю|моя работа|i work as|i work at)\s+([^,.;!?]{2,60})",
            re.IGNORECASE,
        ),
        "job",
    ),
    (
        re.compile(
            r"(?:мой день рождения|my birthday is)\s+([^,.;!?]{2,40})",
            re.IGNORECASE,
        ),
        "birthday",
    ),
    (
        re.compile(
            r"(?:я не люблю|мне не нравится|i don'?t like|i hate)\s+([^,.;!?]{2,60})",
            re.IGNORECASE,
        ),
        "dislikes",
    ),
    (
        re.compile(
            r"(?:я люблю|мне нравится|i like|i love)\s+([^,.;!?]{2,60})",
            re.IGNORECASE,
        ),
        "likes",
    ),
)


class MemorySkill(Skill):
    """Работа с памятью ассистента голосом."""

    meta = SkillMeta(
        name="memory",
        description="Запоминание фактов и предпочтений",
        version="0.2.0",
        spoken=("память", "memory"),
    )

    _journal: str
    _interval: float
    _window: int
    _seen: set[str]
    _task: asyncio.Task[None] | None = None
    _autosave: bool = False
    _last_error: str = ""
    _saved_total: int = 0

    async def on_setup(self) -> None:
        """Запомнить, какие разделы доступны, и поднять фоновый автосбор."""
        self._journal = str(self.context.setting("journal", "today"))
        # Пауза между проходами и размер окна просмотра: окно держим маленьким,
        # потому что фон интересуют только свежие записи.
        self._interval = float(self.context.setting("autosave_interval", 300))
        self._window = int(self.context.setting("autosave_window", 20))
        self._seen = set()
        self._autosave = bool(self.context.setting("autosave", True))
        if self._autosave:
            self._start_autosave()

    async def on_teardown(self) -> None:
        """Погасить фоновую задачу вместе со скиллом."""
        await self._stop_autosave()

    def _start_autosave(self) -> None:
        """Поднять фоновую задачу, если она ещё не работает."""
        if self._task is None or self._task.done():
            # Через scope: при выгрузке и перезагрузке скилла задача гасится сама.
            self._task = self.context.scope.spawn(self._autosave_loop(), name="memory-autosave")

    async def _stop_autosave(self) -> None:
        """Остановить фоновую задачу и дождаться её завершения."""
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _autosave_loop(self) -> None:
        """Периодически разбирать новые записи журнала.

        Ошибку прохода не роняем: фон должен пережить недоступность памяти,
        а о проблеме расскажет health.
        """
        try:
            while True:
                await asyncio.sleep(self._interval)
                try:
                    await self._scan_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._last_error = str(exc)
                else:
                    self._last_error = ""
        except asyncio.CancelledError:
            return

    async def _scan_once(self) -> dict[str, str]:
        """Просмотреть окно журнала и дописать найденное в профиль.

        Возвращает только те факты, которые действительно изменились: лишние
        записи в документ не идут.
        """
        entries = await self.context.memory.recall(
            journal=self._journal,
            limit=self._window,
        )

        window: set[str] = set()
        fresh: list[str] = []
        for entry in entries:
            key = f"{entry.timestamp}|{entry.text}"
            window.add(key)
            if key not in self._seen:
                fresh.append(str(entry.text))
        # Помним только текущее окно: выпавшая запись обратно не вернётся.
        self._seen = window

        if not fresh:
            return {}

        # Разбор синхронный, поэтому уводим его в поток, чтобы голосовой круг
        # не ждал даже на длинном журнале.
        facts = await asyncio.to_thread(self._extract, fresh)
        if not facts:
            return {}

        profile = await self.context.memory.documents.read("profile")
        saved: dict[str, str] = {}
        for key, value in facts.items():
            if str(profile.get(key, "")) == value:
                continue
            await self.context.memory.documents.set("profile", key, value)
            saved[key] = value

        self._saved_total += len(saved)
        return saved

    @staticmethod
    def _extract(texts: list[str]) -> dict[str, str]:
        """Вытащить факты о пользователе из текстов записей.

        Более поздняя запись перебивает более раннюю по тому же ключу.

        :param texts: тексты записей журнала.
        """
        found: dict[str, str] = {}
        for text in texts:
            for pattern, key in _PATTERNS:
                match = pattern.search(text)
                if match is None:
                    continue
                value = _plausible(key, match.group(1).strip(" .,!?;:—-")[:_VALUE_LIMIT])
                if value:
                    found[key] = value
        return found

    @tool(phrases=["запомни {text}", "запиши {text}", "remember {text}",
                   "note that {text}"],
          reversible=False)
    async def remember(self, text: str, tag: str = "note") -> ToolResult:
        """Записать факт в журнал.

        :param text: что запомнить.
        :param tag: метка для последующей выборки.
        """
        try:
            entry = await self.context.memory.remember(text, journal=self._journal, tags=(tag,))
        except Exception as exc:
            return _memory_failure(exc)
        return ToolResult.success(
            {"text": entry.text, "timestamp": entry.timestamp},
            speech={"ru": "Запомнил.", "en": "Noted."},
        )

    @tool(phrases=["что ты помнишь", "напомни что было", "что я просил",
                   "what do you remember", "what did i ask"],
          reversible=True)
    async def recall(self, limit: int = 5, tag: str = "") -> ToolResult:
        """Вспомнить последние записи.

        :param limit: сколько записей вернуть.
        :param tag: показать только записи с этой меткой.
        """
        try:
            entries = await self.context.memory.recall(
                journal=self._journal,
                limit=limit,
                tag=tag or None,
            )
        except Exception as exc:
            return _memory_failure(exc)
        if not entries:
            return ToolResult.success(
                [],
                speech={"ru": "Пока ничего не записано.", "en": "Nothing recorded yet."},
            )

        texts = [str(entry.text) for entry in entries]
        # Вслух — последние одна-две записи отдельными фразами, а не склейка
        # всего подряд через точку с запятой; полный список — в данных ответа.
        spoken = " ".join(text.rstrip(".!? ") + "." for text in texts[-_SPOKEN_RECORDS:])
        rest = len(texts) - _SPOKEN_RECORDS
        more_ru = f" И ещё {rest} {plural_form(rest, ('запись', 'записи', 'записей'))}." if rest > 0 else ""
        more_en = f" And {rest} more." if rest > 0 else ""
        return ToolResult.success(
            texts,
            speech={"ru": f"Вот что помню. {spoken}{more_ru}", "en": f"Here is what I remember. {spoken}{more_en}"},
        )

    @tool(reversible=False)
    async def set_preference(self, key: str, value: str) -> ToolResult:
        """Сохранить устойчивое предпочтение.

        Предпочтения живут в документе, а не в журнале: их правят, а не копят.

        :param key: название настройки.
        :param value: значение.
        """
        try:
            await self.context.memory.documents.set("preferences", key, value)
        except Exception as exc:
            return _memory_failure(exc)
        spoken = _spoken_key(key)
        return ToolResult.success(
            {key: value},
            speech={"ru": f"Записал, {spoken}: {value}.", "en": f"Saved, {key.replace('_', ' ')}: {value}."},
        )

    @tool(phrases=["что ты знаешь обо мне", "what do you know about me"], reversible=True)
    async def about_me(self) -> ToolResult:
        """Показать профиль и предпочтения."""
        try:
            profile = await self.context.memory.documents.read("profile")
            preferences = await self.context.memory.documents.read("preferences")
        except Exception as exc:
            return _memory_failure(exc)
        payload: dict[str, Any] = {"profile": profile, "preferences": preferences}

        if not profile and not preferences:
            return ToolResult.success(
                payload,
                speech={"ru": "Пока я о тебе ничего не знаю.", "en": "I don't know anything about you yet."},
            )

        keys = sorted({**profile, **preferences})
        known_ru = ", ".join(_spoken_key(key) for key in keys)
        known_en = ", ".join(key.replace("_", " ") for key in keys)
        return ToolResult.success(
            payload,
            speech={"ru": f"Знаю про: {known_ru}.", "en": f"I know about: {known_en}."},
        )

    # «Выключи» — отдельной командой: фраза без подстановки не передаёт аргумент,
    # и общая команда с enabled=True по умолчанию на «выключи» включала бы.
    @tool(phrases=["выключи автопамять", "turn autosave off"], routable=False, reversible=True)
    async def autosave_off(self) -> ToolResult:
        """Выключить фоновое пополнение профиля."""
        return await self.autosave(enabled=False)

    @tool(phrases=["включи автопамять", "turn autosave on"], reversible=True)
    async def autosave(self, enabled: bool = True) -> ToolResult:
        """Включить или выключить фоновое пополнение профиля.

        :param enabled: True — собирать факты в фоне, False — только вручную.
        """
        self._autosave = enabled
        if enabled:
            self._start_autosave()
            speech = {"ru": "Буду запоминать сам.", "en": "I'll remember on my own."}
        else:
            await self._stop_autosave()
            speech = {"ru": "Больше сам не записываю.", "en": "Autosave is off."}

        return ToolResult.success(
            {"autosave": enabled, "interval": self._interval},
            speech=speech,
        )

    @tool(phrases=["обнови что знаешь обо мне", "разбери мои записи",
                   "update what you know about me"],
          reversible=False)
    async def digest(self) -> ToolResult:
        """Разобрать свежие записи прямо сейчас, не дожидаясь фона."""
        try:
            saved = await self._scan_once()
        except Exception as exc:
            return ToolResult.failure(
                f"память недоступна: {exc}",
                speech={"ru": "Не смог прочитать журнал.", "en": "I couldn't read the journal."},
            )

        if not saved:
            return ToolResult.success(
                {},
                speech={"ru": "Ничего нового не нашёл.", "en": "Nothing new found."},
            )

        known_ru = ", ".join(_spoken_key(key) for key in sorted(saved))
        known_en = ", ".join(sorted(saved))
        return ToolResult.success(
            saved,
            speech={"ru": f"Добавил в профиль: {known_ru}.", "en": f"Added to profile: {known_en}."},
        )

    async def health(self) -> HealthStatus:
        """Проверить, что журнал доступен и фоновый сбор не отвалился."""
        try:
            await self.context.memory.recall(journal=self._journal, limit=1)
        except Exception as exc:
            return HealthStatus.degraded(f"память недоступна: {exc}")

        if self._last_error:
            return HealthStatus.degraded(f"автосбор сбоит: {self._last_error}")

        if self._autosave and (self._task is None or self._task.done()):
            return HealthStatus.degraded("фоновый автосбор остановлен")

        return HealthStatus.healthy()
