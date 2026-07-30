"""Telegram: прочитать, пересказать, ответить — от своего аккаунта.

Работает через MTProto (Telethon), а не через бота: бот видит только те чаты,
куда его добавили, и переписку с мамой ему не покажут. Отсюда `api_id` и
`api_hash` вместо токена — это ключи приложения, а не доступ к аккаунту.

**Вход в аккаунт делается один раз и руками**, отдельной командой:

    python skills/telegram/login.py

Телеграм присылает код в приложение, его надо ввести. Внутри Jarvis этого не
сделать: голосом код не диктуют, а запуск ассистента не должен зависать в
ожидании ввода. Дальше живёт файл сессии, и он **равносилен входу в аккаунт** —
ни пароля, ни кода к нему не нужно. Поэтому лежит он в `memory/`, который
целиком в `.gitignore`.

Связка двух каналов видна здесь целиком: новое сообщение — это **факт**, он
уходит в шину событием `telegram.message.received`, и слушать его может кто
угодно. А «перескажи переписку» — это **команда** с ответом, и она идёт через
реестр инструментов.

Модель зовётся ровно в одном месте — в пересказе, потому что иначе задачу не
решить. Список чатов и чтение сообщений обходятся без неё: платить за то, что
делается запросом к API, незачем.

**Кому уходит сообщение — решает не распознавание.** Имя из речи проходит путь
микрофон → Whisper → LLM и по дороге меняется; отправить «маме» вместо «Максу»
здесь означает не сбой, а прочитанное чужим человеком письмо. Поэтому имя
сверяется со списком реальных чатов, и при малейшем сомнении Jarvis
переспрашивает, а не угадывает.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, ClassVar, Sequence

from jarvis.core.contracts import Event, ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.text import romanize, skeleton, squash
from jarvis.core.tools import tool

#: Насколько похожим должно быть услышанное имя чата, чтобы считаться тем же.
#: Порог высокий намеренно: цена ошибки — сообщение не тому человеку.
SIMILARITY = 0.8

#: Короче этого сравнивать началом бессмысленно: «ма» подойдёт к половине книги.
MIN_PREFIX = 3

#: Сколько диалогов держать в списке для сопоставления имён.
DIALOG_LIMIT = 100

@dataclass(frozen=True, slots=True, kw_only=True)
class TelegramMessageReceived(Event):
    """Пришло новое сообщение в Telegram.

    Скилл объявляет собственное событие — ядро для этого править не нужно.
    """

    NAME: ClassVar[str] = "telegram.message.received"

    chat: str
    text: str

#: Гласные на конце — по ним и различаются падежи: «маме», «мама», «маму».
_ENDINGS = "аеёиоуыэюяaeiouy"

#: Сколько слов может занимать имя адресата в начале фразы.
_MAX_NAME_WORDS = 5

def _forms(text: str) -> set[str]:
    """Как одно и то же имя может выглядеть: падеж, алфавит, разделители.

    «Маме» и «Мама» — одно имя в разных падежах, «саша» и «Sasha» — в разных
    алфавитах, «Настя Ко» и «настяко» — с разделителем и без. Сравнивать
    поштучно каждый случай значит писать одно и то же четыре раза.
    """
    tight = squash(text)
    latin = squash(romanize(text))
    forms = {tight, tight.rstrip(_ENDINGS), latin, latin.rstrip(_ENDINGS)}
    return {form for form in forms if len(form) >= MIN_PREFIX}

def match_chat(query: str, names: Sequence[str]) -> str | None:
    """Найти чат по услышанному имени.

    Лестница та же, что у названий программ, и по той же причине: услышанное
    редко совпадает с написанным буква в букву. Сначала совпадение любой формы
    имени, потом начало слова («настя» находит «Настя Ко»), потом согласный
    костяк, и только в конце — нечёткое сравнение с высоким порогом.

    :return: имя чата, либо ``None``, если уверенности нет.
    """
    wanted = _forms(query)
    if not wanted:
        return None

    known = {name: _forms(name) for name in names}
    for name, forms in known.items():
        if wanted & forms:
            return name

    starts = [
        name
        for name, forms in known.items()
        if any(form.startswith(part) for form in forms for part in wanted)
    ]
    if starts:
        # Побеждает самое короткое: «мама» — это «Мама», а не «Мама Юли».
        return min(starts, key=len)

    sounds = skeleton(query)
    if len(sounds) >= 4:
        for name in names:
            if skeleton(name) == sounds:
                return name

    tight = squash(query)
    keys = {squash(name): name for name in names}
    close = difflib.get_close_matches(tight, list(keys), n=1, cutoff=SIMILARITY)
    return keys[close[0]] if close else None

def split_request(spoken: str, names: Sequence[str]) -> tuple[str, str]:
    """Разделить «напиши маме буду через час» на адресата и текст.

    Голосом не диктуют двоеточий, поэтому имя и сообщение приходят одной
    строкой. Побеждает то разбиение, где имя совпало **точнее всего**: длина
    услышанного ближе всего к длине настоящего названия. Ни «самое длинное»,
    ни «самое короткое» тут не годятся: первое съедает начало сообщения
    («настя ко я» вместо «Настя Ко»), второе рвёт составные имена.

    :return: пара «имя чата, текст»; имя пустое, если не узнали.
    """
    words = spoken.split()
    best: tuple[tuple[int, int], str, str] | None = None
    for size in range(1, min(len(words), _MAX_NAME_WORDS) + 1):
        head = " ".join(words[:size])
        found = match_chat(head, names)
        if found is None:
            continue
        # Ближе по длине — точнее совпало; при равенстве берём разбиение,
        # где имя длиннее: составные имена важнее случайного слова.
        rank = (abs(len(squash(head)) - len(squash(found))), -size)
        if best is None or rank < best[0]:
            best = (rank, found, " ".join(words[size:]).strip(" ,:—-"))
    return (best[1], best[2]) if best else ("", spoken.strip())

def describe_dialogs(dialogs: Sequence[dict[str, Any]]) -> str:
    """Собрать фразу про непрочитанное — так, как её произносят вслух."""
    if not dialogs:
        return "Новых сообщений нет."
    parts = []
    for item in dialogs:
        count = int(item.get("unread", 0))
        parts.append(f"{item.get('name', '')} — {count}" if count > 1 else str(item.get("name", "")))
    return f"Непрочитано в {len(dialogs)}: " + ", ".join(parts) + "."

class TelegramSkill(Skill):
    """Чтение, пересказ и отправка сообщений Telegram."""

    meta = SkillMeta(
        name="telegram",
        description="Сообщения и чаты Telegram",
        version="0.2.0",
    )

    async def on_setup(self) -> None:
        """Прочитать настройки. Ни сети, ни файлов — здесь только конфиг."""
        self._api_id = int(self.context.setting("api_id", 0) or 0)
        self._api_hash = str(self.context.setting("api_hash", ""))
        self._session = self.context.root / str(
            self.context.setting("session", "memory/telegram.session")
        )
        #: Сообщать ли о новых сообщениях событием в шину.
        self._notify = bool(self.context.setting("notify", True))
        self._history = int(self.context.setting("history", 50))

        self._client: Any = None
        self._names: list[str] = []

        if not self._api_id or not self._api_hash:
            self.log.warning(
                "Telegram не настроен: добавь JARVIS_TELEGRAM_API_ID и "
                "JARVIS_TELEGRAM_API_HASH в .env, ключи берутся на my.telegram.org"
            )

    async def on_start(self) -> None:
        """Подключиться в фоне: старт ассистента не должен ждать сеть."""
        if not self._api_id or not self._api_hash:
            return
        self.context.scope.spawn(self._connect(), name="telegram-connect")

    async def on_stop(self) -> None:
        """Отключиться. Фоновые задачи гасит scope."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001 — на остановке это не важно
                self.log.debug("Telegram отключился с ошибкой: %s", exc)
            self._client = None

    # --- подключение -------------------------------------------------------

    async def _connect(self) -> None:
        """Поднять клиента и, если вход сделан, слушать новые сообщения."""
        try:
            from telethon import TelegramClient, events
        except ImportError:
            self.log.warning(
                "Telethon не установлен — Telegram отключён. "
                'Установи: pip install -e ".[telegram]"'
            )
            return

        if not self._session.exists():
            self.log.warning(
                "Вход в Telegram не сделан. Выполни один раз: "
                "python skills/telegram/login.py"
            )
            return

        self._session.parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(str(self._session.with_suffix("")), self._api_id, self._api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                self.log.warning(
                    "Файл сессии есть, но вход недействителен. Повтори: "
                    "python skills/telegram/login.py"
                )
                await client.disconnect()
                return
        except Exception as exc:  # noqa: BLE001 — сеть падает, ассистент нет
            self.log.warning("Не удалось подключиться к Telegram: %s", exc)
            return

        self._client = client
        me = await client.get_me()
        self.log.info("Telegram подключён: %s", getattr(me, "username", "") or getattr(me, "first_name", ""))

        if self._notify:
            client.add_event_handler(self._on_message, events.NewMessage(incoming=True))

    async def _on_message(self, event: Any) -> None:
        """Новое сообщение — это факт, и он уходит в шину."""
        try:
            chat = await event.get_chat()
            name = getattr(chat, "title", None) or getattr(chat, "first_name", "") or "?"
            self.events.emit(
                TelegramMessageReceived(source="telegram", chat=str(name), text=event.raw_text or "")
            )
        except Exception as exc:  # noqa: BLE001 — сбой обработчика не рвёт связь
            self.log.debug("Не разобрал входящее сообщение: %s", exc)

    async def _ready(self) -> ToolResult | None:
        """Проверить, что с Telegram вообще можно работать."""
        if self._client is not None:
            return None
        if not self._api_id or not self._api_hash:
            return ToolResult.failure(
                "Telegram не настроен: нет api_id и api_hash в .env",
                speech={
                    "ru": "Телеграм не подключён. Добавь ключи в настройки.",
                    "en": "Telegram isn't connected. Add the keys in settings.",
                },
            )
        return ToolResult.failure(
            "Telegram не подключён: вход не сделан или нет сети",
            speech={
                "ru": "Телеграм не подключён. Нужно один раз войти в аккаунт.",
                "en": "Telegram isn't connected. A one-time login is needed.",
            },
        )

    async def _dialogs(self, limit: int = DIALOG_LIMIT) -> list[Any]:
        """Список диалогов; заодно обновляет имена для сопоставления."""
        dialogs = await self._client.get_dialogs(limit=limit)
        self._names = [str(item.name) for item in dialogs if item.name]
        return dialogs

    async def _find(self, chat: str) -> tuple[Any, str] | None:
        """Найти диалог по услышанному имени."""
        dialogs = await self._dialogs()
        name = match_chat(chat, self._names)
        if name is None:
            return None
        return next((item.entity for item in dialogs if item.name == name), None), name

    # --- команды -----------------------------------------------------------

    @tool(phrases=["напиши {request}", "отправь сообщение {request}",
                   "напиши в телеграм {request}", "отправь в телеграм {request}",
                   "send a telegram message {request}"])
    async def send_message(self, request: str, text: str = "") -> ToolResult:
        """Отправить сообщение в чат Telegram.

        :param request: кому писать; если текст не передан отдельно — вместе с
            сообщением, как это и звучит: «напиши маме буду через час».
        :param text: что написать, если адресат назван отдельно.
        """
        if (refusal := await self._ready()) is not None:
            return refusal

        await self._dialogs()
        if text.strip():
            chat, message = request.strip(), text.strip()
        else:
            chat, message = split_request(request, self._names)

        if not chat:
            return ToolResult.failure(
                f"не понял, кому писать: {request!r}",
                speech={
                    "ru": "Не понял, кому написать.",
                    "en": "I didn't catch who to write to.",
                },
            )
        if not message:
            return ToolResult.failure(
                f"пустое сообщение для {chat!r}",
                speech={"ru": f"А что написать {chat}?", "en": f"What should I write to {chat}?"},
            )

        found = await self._find(chat)
        if found is None or found[0] is None:
            # Лучше переспросить, чем отправить письмо не тому человеку.
            close = ", ".join(difflib.get_close_matches(chat, self._names, n=3, cutoff=0.3))
            return ToolResult.failure(
                f"чат {chat!r} не найден" + (f". Похожие: {close}" if close else ""),
                speech={
                    "ru": f"Не нашёл чат {chat}." + (f" Может быть: {close}?" if close else ""),
                    "en": f"No chat named {chat}.",
                },
            )

        entity, name = found
        await self._client.send_message(entity, message)
        self.log.info("Отправлено в %s: %s", name, message)
        return ToolResult.success(
            {"chat": name, "text": message},
            speech={"ru": f"Отправил {name}.", "en": f"Sent to {name}."},
        )

    @tool(phrases=["что нового в телеграме", "проверь телеграм", "новые сообщения",
                   "есть новые сообщения", "any new messages", "check telegram"])
    async def get_recent_chats(self, limit: int = 5) -> ToolResult:
        """Показать чаты с непрочитанными сообщениями.

        :param limit: сколько чатов назвать.
        """
        if (refusal := await self._ready()) is not None:
            return refusal

        dialogs = await self._dialogs()
        unread = [
            {"name": str(item.name), "unread": int(item.unread_count)}
            for item in dialogs
            if item.unread_count
        ][:limit]

        spoken = describe_dialogs(unread)
        return ToolResult.success(
            unread,
            speech={
                "ru": spoken,
                "en": f"Unread chats: {len(unread)}." if unread else "No new messages.",
            },
        )

    @tool(phrases=["прочитай {chat}", "что пишет {chat}", "что написал {chat}",
                   "прочитай сообщения {chat}", "read {chat}"])
    async def read_chat(self, chat: str, limit: int = 5) -> ToolResult:
        """Прочитать последние сообщения из чата.

        :param chat: чей чат читать.
        :param limit: сколько последних сообщений взять.
        """
        if (refusal := await self._ready()) is not None:
            return refusal

        found = await self._find(chat)
        if found is None or found[0] is None:
            return ToolResult.failure(
                f"чат {chat!r} не найден",
                speech={"ru": f"Не нашёл чат {chat}.", "en": f"No chat named {chat}."},
            )

        entity, name = found
        messages = await self._client.get_messages(entity, limit=limit)
        texts = [item.raw_text for item in reversed(messages) if item.raw_text]
        if not texts:
            return ToolResult.success(
                [],
                speech={"ru": f"В чате {name} пусто.", "en": f"Nothing in {name}."},
            )
        return ToolResult.success(
            texts,
            speech={
                "ru": f"{name} пишет: " + ". ".join(texts),
                "en": f"{name} says: " + ". ".join(texts),
            },
        )

    @tool(phrases=["перескажи {chat}", "перескажи переписку {chat}",
                   "о чём пишет {chat}", "summarize {chat}"])
    async def summarize_chat(self, chat: str, limit: int = 0) -> ToolResult:
        """Пересказать переписку в чате.

        Единственное место скилла, где нужна модель: сжать сто сообщений в две
        фразы кодом нельзя. Список чатов и чтение обходятся без неё — платить
        за то, что делается запросом к API, незачем.

        :param chat: чей чат пересказать.
        :param limit: сколько последних сообщений взять; 0 — как в конфиге.
        """
        if (refusal := await self._ready()) is not None:
            return refusal
        if not self.context.llm.available:
            return ToolResult.failure(
                "языковая модель не настроена, а пересказ без неё не сделать",
                speech={
                    "ru": "Пересказывать нечем: модель не подключена.",
                    "en": "No model configured, so I can't summarize.",
                },
            )

        found = await self._find(chat)
        if found is None or found[0] is None:
            return ToolResult.failure(
                f"чат {chat!r} не найден",
                speech={"ru": f"Не нашёл чат {chat}.", "en": f"No chat named {chat}."},
            )

        entity, name = found
        messages = await self._client.get_messages(entity, limit=limit or self._history)
        lines = [
            f"{getattr(item.sender, 'first_name', '') or 'он'}: {item.raw_text}"
            for item in reversed(messages)
            if item.raw_text
        ]
        if not lines:
            return ToolResult.success(
                "",
                speech={
                    "ru": f"В чате {name} нечего пересказывать.",
                    "en": f"Nothing to summarize in {name}.",
                },
            )

        summary = await self.context.llm.summarize("\n".join(lines), sentences=3)
        self.log.info("Пересказал %d сообщений из %s", len(lines), name)
        return ToolResult.success(summary, speech=summary)

    async def health(self) -> HealthStatus:
        """Готовность: настроен ли и подключён ли."""
        if not self._api_id or not self._api_hash:
            return HealthStatus.degraded("нет api_id и api_hash")
        if self._client is None:
            return HealthStatus.degraded("не подключён: нужен вход в аккаунт")
        return HealthStatus.healthy(f"чатов в списке {len(self._names)}")
