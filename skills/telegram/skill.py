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
import logging
import re
from dataclasses import dataclass
from typing import Any, ClassVar, Sequence

from jarvis.core.attention import LOW
from jarvis.core.contracts import Event, ToolResult
from jarvis.core.errors import LLMError
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.text import best_match, squash, starts
from jarvis.core.tools import tool

#: Насколько похожим должно быть услышанное имя чата, чтобы считаться тем же.
#: Порог высокий намеренно: цена ошибки — сообщение не тому человеку.
SIMILARITY = 0.8

#: Короче этого сравнивать началом бессмысленно: «ма» подойдёт к половине книги.
MIN_PREFIX = 3

#: Сколько диалогов держать в списке для сопоставления имён.
DIALOG_LIMIT = 100

#: Сколько текста сообщения проговаривать. Длинное вслух не зачитывают: это
#: уведомление о том, что написали, а не чтение переписки — для чтения есть
#: «прочитай, что пишет мама».
PREVIEW = 80


def announcement(chat: str, text: str) -> str:
    """Как сообщить о входящем вслух."""
    clean = " ".join(text.split())
    if not clean:
        return f"{chat} что-то прислал в телеграме."
    if len(clean) > PREVIEW:
        clean = clean[: PREVIEW - 1].rstrip() + "…"
    return f"{chat} пишет: {clean}"


@dataclass(frozen=True, slots=True, kw_only=True)
class TelegramMessageReceived(Event):
    """Пришло новое сообщение в Telegram.

    Скилл объявляет собственное событие — ядро для этого править не нужно.
    """

    NAME: ClassVar[str] = "telegram.message.received"

    chat: str
    text: str

#: Сколько слов может занимать имя адресата в начале фразы.
_MAX_NAME_WORDS = 5

#: Падежные окончания имён: «Роме», «Эли», «Мишей», «Олей».
_CASE_ENDINGS = ("ой", "ей", "ою", "ею", "ем", "ом", "ам", "ям")


def _base(word: str) -> str:
    """Основа имени без падежного окончания: «эли», «эле», «эля» → «эл»."""
    tight = squash(word)
    for ending in _CASE_ENDINGS:
        if tight.endswith(ending) and len(tight) - len(ending) >= 2:
            return tight[: -len(ending)]
    if len(tight) >= 3 and tight[-1] in "аяеиоуыюь":
        return tight[:-1]
    return tight


def spoken_name(name: str) -> str:
    """Имя чата для речи: без эмодзи и значков («Ромка Малютка ❤️❤️» → «Ромка Малютка»)."""
    clean = re.sub(r"[^\w\s.,'\-]", "", name)
    return " ".join(clean.split()) or name


def match_chat(query: str, names: Sequence[str]) -> str | None:
    """Найти чат по услышанному имени.

    Лестница та же, что у названий программ, и по той же причине: услышанное
    редко совпадает с написанным буква в букву. Сначала совпадение любой формы
    имени, потом начало слова («настя» находит «Настя Ко»), потом согласный
    костяк, и только в конце — нечёткое сравнение с высоким порогом.

    :return: имя чата, либо ``None``, если уверенности нет.
    """
    # Имя из одного слова сперва сверяется по основе, без падежного окончания:
    # «Эли», «Эле» — это «Эля», а не начало «Элины» (живой случай 16.09.2026,
    # 12:05: трижды «не понял, кому», а на соседнем списке «эли» находило
    # «Элину» — сообщение ушло бы не тому человеку).
    if len(query.split()) == 1 and len(_base(query)) >= 2:
        declined = [name for name in names if name.split() and _base(name.split()[0]) == _base(query)]
        if declined:
            return min(declined, key=len)
    return best_match(
        query,
        names,
        similarity=SIMILARITY,
        # Началом, а не любым краем: «Настя» находит «Настя Ко», а вот совпадение
        # концом означало бы, что «Ко» находит её же — фамилия адресата слишком
        # слабое основание, чтобы писать человеку.
        edges=starts,
        least=MIN_PREFIX,
    )

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

#: Глаголы, с которых начинается **поручение**, а не само сообщение: «напиши
#: Роме, спроси как дела» — это просьба спросить, и отправлять слово «спроси»
#: Роме нельзя (живой случай 15.09.2026, 10:35: ушло «спроси как дела и как
#: настроение»).
INDIRECT_VERBS = (
    "спроси", "узнай", "уточни", "скажи", "передай", "поздравь", "попроси",
    "напомни", "поблагодари", "извинись", "предложи", "пригласи", "позови",
    "предупреди", "сообщи", "ask", "tell",
)
_ASKING = ("спроси", "узнай", "уточни", "ask")
_TELLING = ("скажи", "передай", "сообщи", "tell")

#: Как переписать поручение в сообщение. Модель пишет **от первого лица**
#: владельца: только она верно меняет лица («спроси, придёт ли он» → «Придёшь?»).
_REWRITE_PROMPT = (
    "Владелец диктует голосовому ассистенту поручение для сообщения в Telegram "
    "собеседнику «{chat}»: «{message}». Напиши само сообщение так, как его написал "
    "бы владелец: от первого лица, на «ты», коротко и естественно, как пишут в "
    "мессенджере. Передай ровно то, о чём поручение, ничего не добавляй, не "
    "здоровайся, если об этом не просили. Верни только текст сообщения, без кавычек "
    "и пояснений."
)


def indirect_verb(message: str) -> str | None:
    """Глагол поручения в начале текста, если текст — поручение, а не сообщение."""
    words = re.findall(r"[\w'-]+", message.lower())
    if not words:
        return None
    first = words[1] if words[0] in ("и", "а", "and") and len(words) > 1 else words[0]
    return first if first in INDIRECT_VERBS else None


def plain_rewrite(message: str) -> str | None:
    """Переписать поручение без модели — там, где это можно сделать правилом.

    «спроси как дела» → «Как дела?», «скажи, что буду позже» → «Буду позже.».
    Остальное («поздравь», «попроси») правилом не переписать: ``None``.
    """
    verb = indirect_verb(message)
    if verb is None:
        return None
    rest = message.strip()
    rest = re.sub(r"^(и|а|and)\s+", "", rest, flags=re.IGNORECASE)
    rest = rest[len(verb):] if rest.lower().startswith(verb) else rest
    rest = re.sub(r"^[\s,:—-]*(у него|у неё|у нее|его|её|ее|ему|ей|him|her)?[\s,:—-]*", "", rest, flags=re.IGNORECASE)
    if verb in _TELLING:
        rest = re.sub(r"^(что|that)\s+", "", rest, flags=re.IGNORECASE)
    elif verb not in _ASKING:
        return None
    rest = rest.strip(" ,.!?")
    if not rest:
        return None
    ending = "?" if verb in _ASKING else "."
    return rest[0].upper() + rest[1:] + ending


#: Как называют «Избранное» — чат с самим собой. В списке диалогов Telethon он
#: называется именем владельца аккаунта, а не «Избранное», поэтому сопоставлением
#: с названиями его не найти. «Выбранное» — так Deepgram расслышал «Избранное»
#: в живом запуске 14.09.2026; уйти не туда тут нельзя: это свой же чат.
SAVED_MESSAGES = (
    "избранное", "избранные", "сохраненное", "сохранённое", "сохраненные",
    "сохранённые", "saved messages", "saved", "выбранное",
)


def is_saved_messages(chat: str) -> bool:
    """Назван ли чат «Избранное»."""
    return " ".join(chat.lower().split()).strip(" ,.:—-") in SAVED_MESSAGES


def clean_chat(chat: str) -> str:
    """Убрать из услышанного адресата предлоги и «в телеграме».

    Шаблон «отправь скриншот из буфера {chat}» забирает хвост целиком, и в
    живом запуске он был «выбранное в телеграме».
    """
    import re

    text = " ".join(chat.split()).strip(" ,.:—-")
    text = re.sub(r"\s*(в|во)\s+(телеграме?|telegram)$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(в|во|на)\s+", "", text, flags=re.IGNORECASE)
    return text.strip(" ,.:—-")


def describe_dialogs(dialogs: Sequence[dict[str, Any]]) -> str:
    """Собрать фразу про непрочитанное — так, как её произносят вслух."""
    if not dialogs:
        return "Новых сообщений нет."
    parts = []
    for item in dialogs:
        count = int(item.get("unread", 0))
        parts.append(f"{item.get('name', '')} — {count}" if count > 1 else str(item.get("name", "")))
    return f"Непрочитано в {len(dialogs)}: " + ", ".join(parts) + "."

#: Пауза между попытками переподключения после обрыва, секунд.
RETRY_DELAY_S = 10


class TelegramSkill(Skill):
    """Чтение, пересказ и отправка сообщений Telegram."""

    meta = SkillMeta(
        name="telegram",
        description="Сообщения и чаты Telegram",
        version="0.2.2",
        spoken=("телеграм", "telegram"),
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
        #: Проговаривать ли входящие вслух. Отдельно от `notify`: событие в
        #: шине никому не мешает, а речь без вопроса мешает очень.
        self._speak_incoming = bool(self.context.setting("announce", False))
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
        # Библиотека после обрыва сети переподключается сама — по умолчанию раз в
        # секунду и без конца. 17.09.2026 с 09:40 это шло часами: ~350 строк в
        # минуту в логе (20 тысяч за день), а Telegram в ответ начал отказывать
        # «слишком часто» (HTTP 429), что только затягивало круг. Пауза между
        # попытками даёт серверу остыть, а её предупреждения в логе глушатся:
        # о состоянии связи скилл пишет сам.
        logging.getLogger("telethon").setLevel(logging.ERROR)
        client = TelegramClient(
            str(self._session.with_suffix("")),
            self._api_id,
            self._api_hash,
            connection_retries=-1,  # не сдаваться: сеть вернётся
            retry_delay=RETRY_DELAY_S,
            auto_reconnect=True,
        )
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
        """Новое сообщение — это факт: он уходит в шину и, может быть, вслух."""
        try:
            chat = await event.get_chat()
            name = getattr(chat, "title", None) or getattr(chat, "first_name", "") or "?"
            text = event.raw_text or ""
            self.events.emit(
                TelegramMessageReceived(source="telegram", chat=str(name), text=text)
            )
            if self._speak_incoming:
                # Скилл знает **что** случилось; уместно ли сейчас говорить,
                # решает политика. Своего суждения об этом у него нет и быть не
                # должно: иначе каждый источник новостей заведёт своё, и они
                # разъедутся.
                self.context.announcer.offer(
                    announcement(str(name), text),
                    importance=LOW,
                    language="ru",
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
                   "send a telegram message {request}"],
          reversible=False)
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
        dictated = message
        if indirect_verb(message) is not None:
            rewritten = await self._rewrite(message, name)
            if rewritten is None:
                # Отправить само поручение («поздравь её») — хуже, чем переспросить.
                return ToolResult.failure(
                    f"поручение {message!r} не удалось переписать в сообщение",
                    speech={
                        "ru": f"Не понял, что именно написать {name}. Продиктуй сообщение дословно.",
                        "en": f"I couldn't phrase the message to {name}. Please dictate it word for word.",
                    },
                )
            message = rewritten
        await self._client.send_message(entity, message)
        self.log.info("Отправлено в %s: %s", name, message)
        if message != dictated:
            # Текст писал не владелец, а ассистент: вслух — что именно ушло.
            preview = message if len(message) <= PREVIEW else message[: PREVIEW - 1].rstrip() + "…"
            said = spoken_name(name)
            speech = {"ru": f"Отправил {said}: {preview}", "en": f"Sent to {said}: {preview}"}
        else:
            said = spoken_name(name)
            speech = {"ru": f"Отправил {said}.", "en": f"Sent to {said}."}
        return ToolResult.success({"chat": name, "text": message, "dictated": dictated}, speech=speech)

    async def _rewrite(self, message: str, chat: str) -> str | None:
        """Поручение → сообщение от первого лица. Моделью, а без неё — правилом.

        :return: текст сообщения; ``None`` — переписать не удалось, отправлять нельзя.
        """
        llm = getattr(self.context, "llm", None)
        if llm is not None and getattr(llm, "available", False):
            try:
                answer = await llm.ask(_REWRITE_PROMPT.format(chat=chat, message=message), task="dialog")
            except (LLMError, TimeoutError, OSError) as error:
                self.log.warning("Модель не переписала поручение (%s) — пробую правилом", error)
            else:
                text = answer.strip().strip("«»\"'").strip()
                # Модель, начавшая рассуждать, выдаёт простыню; такое не отправляем.
                if text and len(text) <= max(200, len(message) * 4):
                    self.log.info("Поручение %r переписано: %r", message, text)
                    return text
        return plain_rewrite(message)

    @tool(phrases=["отправь скриншот из буфера {chat}", "отправь скриншот из буфера в {chat}",
                   "отправь картинку из буфера {chat}", "отправь картинку из буфера в {chat}",
                   "отправь скриншот в {chat}", "отправь скрин в {chat}",
                   "скинь скриншот в {chat}", "скинь картинку в {chat}"],
          reversible=False)
    async def send_image(self, chat: str, caption: str = "") -> ToolResult:
        """Отправить в чат Telegram картинку из буфера обмена: скриншот, скопированное фото.

        :param chat: кому отправить; «Избранное» — себе.
        :param caption: подпись к картинке, если нужна.
        """
        if (refusal := await self._ready()) is not None:
            return refusal

        # Картинку — первой: это дёшево и без сети, и без неё идти в Telegram
        # незачем. Берётся у скилла буфера по имени инструмента, без импорта.
        grabbed = await self.tools.invoke("clipboard.image")
        picture = grabbed.value if grabbed.ok and isinstance(grabbed.value, dict) else None
        if not picture or not picture.get("png"):
            return ToolResult.failure(
                grabbed.error or "в буфере обмена нет картинки",
                speech={
                    "ru": "В буфере обмена нет картинки. Скопируй скриншот и повтори.",
                    "en": "There's no image in the clipboard. Copy a screenshot and try again.",
                },
            )

        chat = clean_chat(chat)
        if is_saved_messages(chat):
            entity, name = "me", "Избранное"
        else:
            found = await self._find(chat) if chat else None
            if found is None or found[0] is None:
                close = ", ".join(difflib.get_close_matches(chat, self._names, n=3, cutoff=0.3))
                return ToolResult.failure(
                    f"чат {chat!r} не найден" + (f". Похожие: {close}" if close else ""),
                    speech={
                        "ru": f"Не нашёл чат {chat}." + (f" Может быть: {close}?" if close else ""),
                        "en": f"No chat named {chat}.",
                    },
                )
            entity, name = found

        import io

        buffer = io.BytesIO(picture["png"])
        # Расширение Telethon берёт из имени: без него картинка ушла бы файлом.
        buffer.name = "screenshot.png"
        await self._client.send_file(entity, buffer, caption=caption.strip() or None)
        width, height = picture.get("width"), picture.get("height")
        self.log.info("Отправлена картинка в %s (%sx%s)", name, width, height)
        return ToolResult.success(
            {"chat": name, "width": width, "height": height},
            speech={"ru": f"Отправил картинку в {spoken_name(name)}.", "en": f"Sent the image to {spoken_name(name)}."},
        )

    @tool(phrases=["что нового в телеграме", "проверь телеграм", "новые сообщения",
                   "есть новые сообщения", "any new messages", "check telegram"],
          reversible=True)
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
                   "прочитай сообщения {chat}", "read {chat}"],
          reversible=True)
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
                   "о чём пишет {chat}", "summarize {chat}"],
          reversible=True)
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
