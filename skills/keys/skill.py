"""Клавиатура как вход и как выход: наблюдатель, реакции и голосовой ввод.

Три способности в одном скилле, все про клавиатуру:

* **Наблюдатель.** «Пишу "курс рубля", и Джарвис отвечает, не дожидаясь Enter».
  Вейкворд для клавиатуры: набранное копится в коротком буфере, буфер сверяется
  с закрытым списком фраз, совпадение уходит в систему как сказанная вслух
  команда (событие `CommandTyped`).
* **Реакции.** Живость: на набранные слова («не работает», «дедлайн») ассистент
  роняет ироничную реплику. Это речь, о которой не просили, поэтому она идёт
  через политику `Announcer` — та и не даёт острить чаще раза в минуту. Список
  реплик местный: наружу уходит только совпавшая фраза, не весь набор.
* **Голосовой ввод.** «Впиши …», «набери …» — ассистент печатает продиктованное
  в активное поле через SendInput. Enter не жмёт: вписать — его дело, отправить
  решает человек. Свой же ввод помечен как «вставленный» и наблюдателем
  пропускается, иначе ассистент среагировал бы на то, что напечатал сам.

Устройство и границы, которые тут важнее кода:

* **Закрытый список триггеров, а не запись всего.** Джарвис реагирует только на
  твои фразы. Остальной набор проходит сквозь короткий буфер (последние
  несколько десятков символов) и тут же забывается: на диск и в лог не пишется
  никогда. Это и есть разница между «вейквордом для клавиатуры» и настоящим
  логгером нажатий.
* **Сопоставление местное.** Триггеры ловятся сравнением строк в Python, без
  сети и без модели. Наружу уходит только сработавшая команда — тем же
  событием `CommandTyped`, что понимает голосовой конвейер, — а он проводит её
  через тот же роутер. «Новый вход не даёт новых прав» соблюдается буквально.
* **Владелец выбрал охват «вся система».** Значит, в буфер на уровне ОС попадают
  и пароли, и номера карт до маскировки. Защита от хранения — закрытый список и
  короткий буфер; защита от реакции в чужом месте — пропуск окон из
  `skip_windows` (менеджеры паролей, банки). Совсем читать их всё же можно;
  единственная твёрдая гарантия здесь — что реагируем мы лишь на свои фразы.
* **Выключено по умолчанию** (`keys.enabled`). И гасится на ходу голосом:
  «перестань следить за клавиатурой» — важный предохранитель для такой штуки.

Захват — тонкая обёртка над низкоуровневым хуком Windows и проверяется живьём,
как у скилла `windows`. Вся логика (буфер, совпадение, пропуск окна) — чистые
функции, и на них тесты.
"""

from __future__ import annotations

import asyncio
import ctypes
import sys
import threading
import time
from collections.abc import Callable, Mapping
from ctypes import wintypes
from typing import Any, NamedTuple

from jarvis.core.attention import LOW
from jarvis.core.contracts import CommandTyped, ToolResult
from jarvis.core.errors import LLMError
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.state import DEAF
from jarvis.core.tools import tool

#: Ошибки модели, при которых текст впечатывается дословно, а не теряется.
LLMErrors = (LLMError, TimeoutError, OSError)

#: Как причёсывать продиктованное. Модель переписывает, а не отвечает: слышит
#: она рваную устную речь, а вернуть должна аккуратное сообщение и ничего сверх.
_POLISH = (
    "Ты — редактор. Тебе дают надиктованный вслух текст: рваный, с оговорками, "
    "без знаков препинания. Перепиши его в аккуратное, грамотное сообщение на "
    "том же языке. Сохрани смысл и все факты, не добавляй ничего от себя, не "
    "отвечай на текст и не комментируй. Верни ТОЛЬКО переписанный текст, без "
    "кавычек и пояснений."
)

#: Запрос модели на живую реакцию (когда включён `react_llm`). Просим одну
#: короткую ироничную реплику в характере Джарвиса, а не ответ по существу.
_REACT_PROMPT = (
    "Пользователь печатает за компьютером. Вот последнее, что он набрал: "
    "«{context}». Оброни ОДНУ короткую ироничную реплику в стиле Джарвиса из "
    "«Железного человека»: сдержанно, с достоинством, можно «сэр». Только "
    "реплику, без пояснений и кавычек."
)

#: Сколько последних символов держать в буфере, если в конфиге не сказано иное.
#: Больше самой длинной фразы с запасом — и не больше: буфер не архив набранного,
#: а окно ровно чтобы поймать фразу на стыке нажатий.
DEFAULT_WINDOW = 64

#: Сколько молчать по одному и тому же триггеру после срабатывания, секунд.
#: Без паузы удержанная клавиша или повтор фразы выстрелили бы очередью.
DEFAULT_COOLDOWN = 4.0

#: Слежение выключено, пока владелец явно не включит. Гарантия в коде, а не в
#: конфиге: пустой конфиг на свежей машине не должен поднять кейлоггер молча.
DEFAULT_ENABLED = False

#: Триггеры по умолчанию: фраза → команда, которую отдать роутеру. Команда — то
#: же, что сказал бы вслух: она пойдёт через ту же цепочку резолверов.
DEFAULT_TRIGGERS: dict[str, str] = {
    "курс рубля": "курс рубля",
    "курс доллара": "курс доллара",
    "курс евро": "курс евро",
    "какая погода": "какая погода",
}

class Reaction(NamedTuple):
    """Что сработало на наборе: слово, готовая реплика и недавний контекст."""

    keyword: str
    quip: str
    context: str


#: Реакции по умолчанию: подстрока в наборе → ироничные реплики, из которых
#: выбирается по очереди. Это не команды: их ассистент говорит сам, поэтому они
#: идут через политику речи без вопроса (пауза между репликами, тихие часы) —
#: она и не даёт ему острить каждые несколько секунд. Список большой намеренно:
#: с пятью словами живость быстро приедается. Правится в конфиге под свой стиль.
DEFAULT_REACTIONS: dict[str, tuple[str, ...]] = {
    "не работает": ("Как всегда, сэр.", "Ожидаемо.", "Опять оно."),
    "не запускается": ("Классика жанра, сэр.", "Ну разумеется."),
    "почему": ("Хороший вопрос, сэр.", "Вот и я думаю."),
    "дедлайн": ("Звучит напряжённо.", "Оптимистично, сэр."),
    "устал": ("Держитесь, сэр.", "Может, перерыв?"),
    "кофе": ("Отличная идея, сэр.", "Одобряю."),
    "гений": ("Скромность украшает, сэр.",),
    "не понимаю": ("Присоединяюсь, сэр.", "Мы оба, сэр."),
    "баг": ("Это не баг, сэр.", "Фича, я полагаю."),
    "ошибка": ("Бывает у лучших, сэр.", "Экспериментально."),
    "получилось": ("Поздравляю, сэр.", "Не сомневался."),
    "готово": ("Впечатляет, сэр.",),
    "наконец": ("Терпение вознаграждается, сэр.",),
    "сдаюсь": ("Рано, сэр.", "Ещё один заход?"),
    "переделать": ("С удовольствием, сэр.", "Ну конечно."),
    "лень": ("Понимаю, сэр.", "Кто бы говорил."),
    "гениально": ("Не буду спорить, сэр.",),
    "не помню": ("Для этого есть я, сэр.",),
    "срочно": ("Как обычно, сэр.",),
    "завтра": ("Знакомое слово, сэр.",),
    "потом": ("То есть никогда, сэр?",),
    "зависло": ("Терпение, сэр.", "Дайте ему минуту."),
    "паника": ("Спокойствие, сэр.", "Только спокойствие."),
    "идеально": ("Как и всё у вас, сэр.",),
    "ненавижу": ("Сильно сказано, сэр.",),
    "работает": ("Не трогайте, сэр.", "Вот и славно."),
}

#: Окна, в которых не следим вовсе: их заголовки выдают ввод, который не должен
#: попадать даже в короткий буфер. Совпадение — по подстроке в заголовке.
DEFAULT_SKIP = (
    "bitwarden",
    "keepass",
    "1password",
    "lastpass",
    "парол",  # стеблем, чтобы ловить и «пароль», и «пароля», и «пароли»
    "password",
    "банк",
    "bank",
    "сбербанк",
    "тинькофф",
    "tinkoff",
)


def normalize(text: str) -> str:
    """Привести к виду для сравнения: нижний регистр, одиночные пробелы."""
    return " ".join(text.split()).lower()


def is_sensitive(title: str, patterns: tuple[str, ...]) -> bool:
    """Похоже ли активное окно на то, где следить нельзя.

    По заголовку, а не по процессу: браузерная вкладка банка и менеджер паролей
    подписаны в заголовке, а тащить ради этого перечисление процессов на каждое
    нажатие незачем. Проверка грубая и честно таковой остаётся — твёрдую
    гарантию даёт закрытый список триггеров, а не этот фильтр.
    """
    low = title.lower()
    return any(mark in low for mark in patterns)


class Triggers:
    """Скользящий буфер набранного и совпадение с закрытым списком фраз.

    Чистый, без всякой ОС: на нём и держится проверяемость. Копит символы,
    держит последние `window`, после каждого сверяет хвост с фразами и, совпав,
    отдаёт команду и очищается — чтобы то же самое не сработало второй раз с
    продолжением набора.
    """

    def __init__(
        self,
        mapping: Mapping[str, str],
        *,
        window: int = DEFAULT_WINDOW,
        cooldown_s: float = DEFAULT_COOLDOWN,
    ) -> None:
        #: Нормализованная фраза → команда. Пустые ключи выкидываем сразу.
        self._commands = {
            normalize(phrase): command
            for phrase, command in mapping.items()
            if phrase.strip()
        }
        #: Длинные фразы проверяем первыми: «курс доллара» должен побеждать
        #: «курс», иначе более общий триггер перехватит более точный.
        self._phrases = sorted(self._commands, key=len, reverse=True)
        longest = max((len(phrase) for phrase in self._phrases), default=0)
        #: Окно не короче самой длинной фразы плюс запас — иначе её не поймать.
        self._window = max(window, longest + 8)
        self._cooldown = max(0.0, cooldown_s)
        self._buffer = ""
        self._fired: dict[str, float] = {}

    @property
    def phrases(self) -> tuple[str, ...]:
        """Отслеживаемые фразы, длинные первыми."""
        return tuple(self._phrases)

    def reset(self) -> None:
        """Забыть накопленное: новая строка, чужое окно, сработавший триггер."""
        self._buffer = ""

    def backspace(self) -> None:
        """Стереть последний символ — Backspace правит и наш буфер тоже."""
        self._buffer = self._buffer[:-1]

    def feed(self, char: str, *, now: float | None = None) -> str | None:
        """Добавить символ и проверить, не сложилась ли фраза.

        :param char: один печатный символ (уже переведённый из нажатия).
        :param now: текущее время; по умолчанию — сейчас. Для тестов.
        :return: команду для роутера, если хвост буфера совпал с триггером и тот
            не на паузе; иначе ``None``.
        """
        if not char:
            return None
        self._buffer = (self._buffer + char.lower())[-self._window :]
        moment = time.monotonic() if now is None else now
        for phrase in self._phrases:
            if not self._buffer.endswith(phrase):
                continue
            last = self._fired.get(phrase)
            if last is not None and moment - last < self._cooldown:
                # На паузе: тот же триггер только что срабатывал. Буфер всё
                # равно чистим, чтобы он не «висел» совпавшим до конца паузы.
                self.reset()
                return None
            self._fired[phrase] = moment
            self.reset()
            return self._commands[phrase]
        return None

    def feed_word(self, word: str, *, now: float | None = None) -> str | None:
        """Подать слово целиком — удобный ярлык поверх `feed`.

        :return: команду, если по ходу набора сложился триггер; иначе ``None``.
        """
        result: str | None = None
        for char in word:
            got = self.feed(char, now=now)
            if got is not None:
                result = got
        return result


class Reactions:
    """То же совпадение по буферу, но ответ — ироничная реплика, не команда.

    Отличий от `Triggers` два, и оба по делу. Во-первых, на одну подстроку
    приходится несколько реплик, и они выдаются по кругу — иначе живость
    оборачивается попугаем. Во-вторых, пауза по умолчанию длиннее: острить на
    каждое «почему» невыносимо, а редко — забавно. Глобально частоту всё равно
    держит политика речи без вопроса, у которой своя пауза между репликами.
    """

    def __init__(
        self,
        mapping: Mapping[str, tuple[str, ...] | list[str]],
        *,
        window: int = DEFAULT_WINDOW,
        cooldown_s: float = 60.0,
    ) -> None:
        self._quips = {
            normalize(pattern): tuple(quips)
            for pattern, quips in mapping.items()
            if pattern.strip() and quips
        }
        self._patterns = sorted(self._quips, key=len, reverse=True)
        longest = max((len(pattern) for pattern in self._patterns), default=0)
        self._window = max(window, longest + 8)
        self._cooldown = max(0.0, cooldown_s)
        self._buffer = ""
        self._fired: dict[str, float] = {}
        #: Какую реплику выдали прошлый раз — чтобы идти по кругу, а не повторять.
        self._turn: dict[str, int] = {}

    @property
    def patterns(self) -> tuple[str, ...]:
        """Отслеживаемые подстроки, длинные первыми."""
        return tuple(self._patterns)

    def reset(self) -> None:
        """Забыть накопленное — новая строка или чужое окно."""
        self._buffer = ""

    def backspace(self) -> None:
        """Стереть последний символ."""
        self._buffer = self._buffer[:-1]

    def feed(self, char: str, *, now: float | None = None) -> "Reaction | None":
        """Добавить символ и, если сложилась подстрока, вернуть реакцию.

        Возвращается не только реплика, но и совпавшее слово и недавний набор:
        для готовой реплики хватит первого, а модель, если её включили, сочинит
        по контексту. Буфер, в отличие от триггеров, **не чистится** после
        срабатывания: подстрока живёт внутри слов, стирать контекст незачем — от
        повтора защищает пауза.
        """
        if not char:
            return None
        self._buffer = (self._buffer + char.lower())[-self._window :]
        moment = time.monotonic() if now is None else now
        for pattern in self._patterns:
            if not self._buffer.endswith(pattern):
                continue
            last = self._fired.get(pattern)
            if last is not None and moment - last < self._cooldown:
                return None
            self._fired[pattern] = moment
            quips = self._quips[pattern]
            index = self._turn.get(pattern, -1) + 1
            self._turn[pattern] = index
            return Reaction(
                keyword=pattern, quip=quips[index % len(quips)], context=self._buffer
            )
        return None

    def feed_pattern(self, text: str, *, now: float | None = None) -> "Reaction | None":
        """Подать подстроку целиком — ярлык поверх `feed` для тестов и удобства.

        :return: реакцию, если по ходу набора сложилась подстрока; иначе ``None``.
        """
        result: "Reaction | None" = None
        for char in text:
            got = self.feed(char, now=now)
            if got is not None:
                result = got
        return result


#: Слова, которыми диктующий просит вписать буквально, без переписывания.
_VERBATIM_MARKERS = ("дословно", "буквально", "как есть", "verbatim")


def strip_verbatim_marker(text: str) -> tuple[bool, str]:
    """Отделить пометку «дословно» в начале продиктованного.

    «Впиши дословно …» — просьба не причёсывать, а набрать как сказано. Метку
    убираем, остальное возвращаем как есть.

    :return: пара «просили дословно» и текст без метки.
    """
    stripped = text.lstrip(" ,.")
    low = stripped.lower()
    for marker in _VERBATIM_MARKERS:
        if low.startswith(marker):
            rest = stripped[len(marker) :].lstrip(" ,.:—-")
            if rest:
                return True, rest
    return False, text


def unicode_events(text: str) -> list[tuple[int, bool]]:
    """Разложить текст на события клавиатуры для ввода в поле.

    Печатаем через юникод-события (KEYEVENTF_UNICODE): так символ попадает в
    поле как есть, независимо от раскладки — кириллице это необходимо. Каждый
    символ — код-юнит(ы) UTF-16, и на каждый два события: нажать и отпустить.
    Символы вне BMP (эмодзи) занимают два код-юнита — суррогатную пару, — и оба
    должны уйти, иначе вставится половина.

    :return: список пар ``(код-юнит, отпускание ли)``.
    """
    events: list[tuple[int, bool]] = []
    for char in text:
        blob = char.encode("utf-16-le")
        for i in range(0, len(blob), 2):
            unit = int.from_bytes(blob[i : i + 2], "little")
            events.append((unit, False))
            events.append((unit, True))
    return events


# --- Захват: только Windows, проверяется живьём -----------------------------

#: Низкоуровневый хук клавиатуры и коды сообщений — из WinUser.h.
_WH_KEYBOARD_LL = 13
_WM_KEYDOWN = 0x0100
_WM_SYSKEYDOWN = 0x0104
_WM_QUIT = 0x0012
_VK_BACK = 0x08
_VK_RETURN = 0x0D
_VK_SHIFT = 0x10
_VK_CAPITAL = 0x14
#: Событие клавиатуры вставлено программой, а не человеком. Свой же ввод (диктовка
#: ниже) приходит с этим флагом — и его надо пропускать, иначе ассистент
#: среагирует на то, что напечатал сам.
_LLKHF_INJECTED = 0x10
#: Ввод текста в поле: юникод-символ, нажать и отпустить.
_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
#: Как часто перепроверять, не сменилось ли активное окно, секунд. На каждое
#: нажатие спрашивать ОС расточительно, а полсекунды хватает.
_FOREGROUND_TTL = 0.5


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    """Что ОС кладёт по указателю lParam при событии клавиатуры (WinUser.h)."""

    _fields_ = (
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _KEYBDINPUT(ctypes.Structure):
    """Одно событие клавиатуры для SendInput."""

    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _MOUSEINPUT(ctypes.Structure):
    """Не используется, но задаёт размер объединения INPUT — оно по мыши."""

    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT))


class _INPUT(ctypes.Structure):
    """INPUT для SendInput. Размер объединения обязан быть от большего члена —
    иначе на 64 бит структура короче, чем ждёт ОС, и ввод молча не проходит."""

    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


def type_text_os(text: str) -> int:
    """Впечатать текст в активное поле через SendInput. Возвращает число событий.

    Отдельная от хука функция и свой дескриптор user32: диктовать можно и при
    выключенном наблюдателе. Enter не жмём намеренно — человек сам решит,
    отправлять ли; наша задача только вписать.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.restype = wintypes.UINT
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]

    events = unicode_events(text)
    if not events:
        return 0
    array = (_INPUT * len(events))()
    for slot, (unit, is_up) in zip(array, events, strict=True):
        slot.type = _INPUT_KEYBOARD
        slot.u.ki = _KEYBDINPUT(
            wVk=0,
            wScan=unit,
            dwFlags=_KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if is_up else 0),
            time=0,
            dwExtraInfo=None,
        )
    sent = user32.SendInput(len(array), array, ctypes.sizeof(_INPUT))
    return int(sent)


def _configure(user32: Any, kernel32: Any) -> None:
    """Объявить типы функций WinAPI.

    Без этого ctypes считает, что всё возвращает 32-битный ``int``, и на
    64-битном Python **обрезает указатели**: дескриптор раскладки, окна и хука
    превращаются в мусор, а хук просто не встаёт. Ошибка тихая, поэтому типы
    проставляем явно. Дескрипторы держим как ``c_void_p``: не все из них есть в
    ``wintypes`` под всеми версиями, а размер у всех один.
    """
    handle = ctypes.c_void_p
    user32.SetWindowsHookExW.restype = handle
    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, ctypes.c_void_p, handle, wintypes.DWORD,
    ]
    user32.CallNextHookEx.restype = wintypes.LPARAM
    user32.CallNextHookEx.argtypes = [
        handle, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.UnhookWindowsHookEx.argtypes = [handle]
    user32.GetForegroundWindow.restype = handle
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [handle, ctypes.c_void_p]
    user32.GetKeyboardLayout.restype = handle
    user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
    user32.MapVirtualKeyExW.restype = wintypes.UINT
    user32.MapVirtualKeyExW.argtypes = [wintypes.UINT, wintypes.UINT, handle]
    user32.ToUnicodeEx.restype = ctypes.c_int
    user32.ToUnicodeEx.argtypes = [
        wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
        ctypes.c_wchar_p, ctypes.c_int, wintypes.UINT, handle,
    ]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [handle]
    user32.GetWindowTextW.argtypes = [handle, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetMessageW.argtypes = [
        ctypes.c_void_p, handle, wintypes.UINT, wintypes.UINT,
    ]
    user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    kernel32.GetModuleHandleW.restype = handle
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD


class KeyboardWatcher:
    """Глобальный хук клавиатуры: переводит нажатия в символы и ловит фразы.

    Живёт на отдельном потоке ОС со своим циклом сообщений — иначе
    низкоуровневый хук не работает. При совпадении зовёт `on_command` (уже из
    потока asyncio, через переданный `to_loop`), а не лезет в шину сам.
    """

    def __init__(
        self,
        triggers: Triggers,
        *,
        on_command: Callable[[str], None],
        to_loop: Callable[[Callable[[], None]], None],
        reactions: Reactions | None = None,
        on_react: Callable[[Reaction], None] | None = None,
        skip: tuple[str, ...] = DEFAULT_SKIP,
    ) -> None:
        self._triggers = triggers
        self._on_command = on_command
        self._to_loop = to_loop
        self._reactions = reactions
        self._on_react = on_react
        self._skip = skip
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._hook: Any = None  # HHOOK, живёт в ОС
        self._callback: Any = None  # ссылку держим сами, иначе соберёт GC
        self._user32: Any = None
        #: Состояние клавиатуры для ToUnicodeEx: 256 байт, мы сами ведём Shift и
        #: Caps — на потоке хука GetKeyboardState им верить нельзя.
        self._keystate: Any = None
        self._sensitive_at = 0.0
        self._sensitive = False

    @property
    def running(self) -> bool:
        """Установлен ли хук прямо сейчас."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Поднять поток с хуком. Повторный вызов ничего не делает."""
        if self.running:
            return
        self._thread = threading.Thread(
            target=self._run, name="keys-hook", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Снять хук и остановить поток сообщений."""
        if self._thread_id and self._user32 is not None:
            self._user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None
        self._thread_id = 0

    # --- поток хука --------------------------------------------------------

    def _run(self) -> None:
        """Тело потока: поставить хук и крутить цикл сообщений до WM_QUIT."""
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure(user32, kernel32)
        self._user32 = user32
        self._keystate = (ctypes.c_ubyte * 256)()
        self._thread_id = kernel32.GetCurrentThreadId()

        proc_type = ctypes.CFUNCTYPE(
            ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        )

        def callback(code: int, wparam: int, lparam: int) -> int:
            if code == 0:  # HC_ACTION: событие можно читать
                try:
                    self._on_event(wparam, lparam)
                except Exception:  # noqa: BLE001 — хук не имеет права падать
                    pass
            return user32.CallNextHookEx(self._hook, code, wparam, lparam)

        # Ссылку держим на себе: соберёт сборщик — ОС позовёт мёртвый код.
        self._callback = proc_type(callback)
        self._hook = user32.SetWindowsHookExW(
            _WH_KEYBOARD_LL, self._callback, kernel32.GetModuleHandleW(None), 0
        )
        if not self._hook:
            self._thread_id = 0
            return

        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            pass  # хук работает в колбэке; нам нужен лишь живой цикл сообщений
        user32.UnhookWindowsHookEx(self._hook)
        self._hook = None

    def _on_event(self, wparam: int, lparam: int) -> None:
        """Разобрать одно событие клавиатуры."""
        info = ctypes.cast(
            ctypes.c_void_p(lparam), ctypes.POINTER(_KBDLLHOOKSTRUCT)
        ).contents
        if info.flags & _LLKHF_INJECTED:
            # Свой же ввод (диктовка) или чужая программа — не человек. Иначе
            # ассистент среагировал бы на то, что напечатал сам.
            return
        vk = int(info.vkCode)
        down = wparam in (_WM_KEYDOWN, _WM_SYSKEYDOWN)

        # Модификаторы ведём сами, и на нажатие, и на отпускание.
        if vk in (_VK_SHIFT, 0xA0, 0xA1):
            self._keystate[_VK_SHIFT] = 0x80 if down else 0x00
            return
        if vk == _VK_CAPITAL and down:
            self._keystate[_VK_CAPITAL] ^= 0x01
            return
        if not down:
            return

        if vk == _VK_BACK:
            self._triggers.backspace()
            if self._reactions is not None:
                self._reactions.backspace()
            return
        if vk == _VK_RETURN:
            # Enter завершает строку. До него мы и реагируем — в этом вся суть,
            # — а после него начинаем с чистого листа.
            self._triggers.reset()
            if self._reactions is not None:
                self._reactions.reset()
            return

        if self._foreground_sensitive():
            # В чужом окне (банк, менеджер паролей) не копим вовсе.
            return

        char = self._translate(vk)
        if not char:
            return
        command = self._triggers.feed(char)
        if command is not None:
            # Команда важнее шутки: одно нажатие не делает и то, и другое.
            self._to_loop(lambda: self._on_command(command))
            if self._reactions is not None:
                self._reactions.reset()
            return
        if self._reactions is not None and self._on_react is not None:
            reaction = self._reactions.feed(char)
            if reaction is not None:
                self._to_loop(lambda: self._on_react(reaction))

    def _translate(self, vk: int) -> str:
        """Перевести виртуальную клавишу в символ с учётом раскладки и Shift."""
        user32 = self._user32
        hwnd = user32.GetForegroundWindow()
        thread_id = user32.GetWindowThreadProcessId(hwnd, None)
        layout = user32.GetKeyboardLayout(thread_id)
        scan = user32.MapVirtualKeyExW(vk, 0, layout)  # MAPVK_VK_TO_VSC
        buffer = (ctypes.c_wchar * 8)()
        count = user32.ToUnicodeEx(
            vk, scan, self._keystate, buffer, len(buffer), 0, layout
        )
        if count == 1:
            return str(buffer[0])
        return ""

    def _foreground_sensitive(self) -> bool:
        """Активное окно из списка запретных? Ответ кешируется на полсекунды."""
        now = time.monotonic()
        if now - self._sensitive_at < _FOREGROUND_TTL:
            return self._sensitive
        user32 = self._user32
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value
        self._sensitive = is_sensitive(title, self._skip)
        self._sensitive_at = now
        return self._sensitive


class KeysSkill(Skill):
    """Следит за набранным и отдаёт совпавшие фразы роутеру как команды."""

    meta = SkillMeta(
        name="keys",
        description="Ловит набранные ключевые фразы и отвечает, не дожидаясь Enter.",
        version="0.1.0",
        platforms=("windows",),
        spoken=("клавиатура", "keyboard"),
    )

    def __init__(self) -> None:
        super().__init__()
        self._triggers: Triggers | None = None
        self._reactions: Reactions | None = None
        self._watcher: KeyboardWatcher | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._enabled = False

    async def on_setup(self) -> None:
        """Собрать триггеры, реакции и наблюдателя из настроек."""
        mapping = self.context.setting("triggers", DEFAULT_TRIGGERS) or {}
        window = int(self.context.setting("window", DEFAULT_WINDOW))
        cooldown = float(self.context.setting("cooldown_seconds", DEFAULT_COOLDOWN))
        skip = tuple(self.context.setting("skip_windows", list(DEFAULT_SKIP)))
        self._enabled = bool(self.context.setting("enabled", DEFAULT_ENABLED))
        react = bool(self.context.setting("react", True))
        quips = self.context.setting("reactions", DEFAULT_REACTIONS) or {}
        #: Переписывать ли продиктованное моделью (иначе — буквально).
        self._rewrite = bool(self.context.setting("rewrite", True))
        #: Сочинять ли реакции моделью. Дороже и отправляет недавний набор в
        #: облако при срабатывании ключа, поэтому по умолчанию выключено.
        self._react_llm = bool(self.context.setting("react_llm", False))

        self._triggers = Triggers(mapping, window=window, cooldown_s=cooldown)
        self._reactions = Reactions(quips, window=window) if react and quips else None
        if sys.platform == "win32":
            self._watcher = KeyboardWatcher(
                self._triggers,
                on_command=self._dispatch,
                to_loop=self._from_thread,
                reactions=self._reactions,
                on_react=self._react,
                skip=skip,
            )
        self.log.info(
            "Клавиатурный наблюдатель: %s, триггеров %d, реакций %d",
            "включён" if self._enabled else "выключен",
            len(self._triggers.phrases),
            len(self._reactions.patterns) if self._reactions else 0,
        )

    async def on_start(self) -> None:
        """Поднять хук, если он включён и мы на Windows."""
        self._loop = asyncio.get_running_loop()
        if self._enabled and self._watcher is not None:
            self._watcher.start()
            self.log.info("Слежу за клавиатурой: %s", ", ".join(self._triggers.phrases))

    async def on_stop(self) -> None:
        """Снять хук."""
        if self._watcher is not None and self._watcher.running:
            await asyncio.to_thread(self._watcher.stop)

    # --- мост между потоком хука и циклом ----------------------------------

    def _from_thread(self, action: Callable[[], None]) -> None:
        """Перебросить действие с потока хука в цикл asyncio."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(action)

    def _dispatch(self, command: str) -> None:
        """Отдать совпавшую фразу роутеру — уже из потока цикла.

        В режиме «не слушаю» молчим: просили не отвлекать, и набранное так же
        не повод заговорить, как и услышанное.
        """
        if self.modes.active(DEAF):
            self.log.debug("Набрано %r, но сейчас не слушаю", command)
            return
        self.log.info("Сработал триггер: %r", command)
        self.events.emit(CommandTyped(source="keyboard", text=command))

    def _react(self, reaction: Reaction) -> None:
        """Ироничная реплика на набранное — через политику речи без вопроса.

        Это не команда: ассистента об этом не просили. Поэтому реплика идёт в
        `Announcer` важностью `LOW` и с `hold=False` — уместна только сейчас,
        держать её на потом смысла нет. Пауза между репликами и тихие часы —
        забота политики; здесь мы лишь предлагаем.

        Готовая реплика — местная. Если включён `react_llm`, вместо неё модель
        сочиняет реплику по недавнему контексту: живее, но дороже и отправляет
        набранное в облако — потому по умолчанию выключено и только по ключу.
        """
        if self.modes.active(DEAF):
            return
        if self._react_llm and self.context.llm.available:
            self.context.scope.spawn(
                self._react_with_llm(reaction), name="keys-react"
            )
            return
        self.context.announcer.offer(reaction.quip, importance=LOW, hold=False)

    async def _react_with_llm(self, reaction: Reaction) -> None:
        """Сочинить реплику моделью по контексту. Сбой — готовая реплика."""
        prompt = _REACT_PROMPT.format(context=reaction.context.strip())
        try:
            line = await self.context.llm.ask(prompt, task="dialog")
        except LLMErrors as error:
            self.log.debug("Модель реакцию не дала (%s) — беру готовую", error)
            line = reaction.quip
        line = line.strip().strip("«»\"'").strip() or reaction.quip
        self.context.announcer.offer(line, importance=LOW, hold=False)

    # --- голосовые переключатели ------------------------------------------

    @tool(
        name="watch",
        phrases=[
            "следи за клавиатурой",
            "следи за тем что я печатаю",
            "следи что я набираю",
            "watch the keyboard",
        ],
        reversible=False,
    )
    async def watch(self) -> str:
        """Включить слежение за набранным на клавиатуре."""
        if self._watcher is None:
            return "Следить за клавиатурой я умею только на Windows."
        self._enabled = True
        if not self._watcher.running:
            self._watcher.start()
        return "Слежу за клавиатурой, сэр."

    @tool(
        name="unwatch",
        phrases=[
            "перестань следить за клавиатурой",
            "не следи за клавиатурой",
            "хватит следить за тем что я печатаю",
            "stop watching the keyboard",
        ],
        reversible=True,
    )
    async def unwatch(self) -> str:
        """Выключить слежение за клавиатурой."""
        self._enabled = False
        if self._watcher is not None and self._watcher.running:
            await asyncio.to_thread(self._watcher.stop)
        return "Больше не слежу за клавиатурой."

    @tool(name="status", routable=False, reversible=True)
    async def status(self) -> dict:
        """Сообщить, следит ли ассистент за клавиатурой и за какими фразами."""
        watching = self._watcher is not None and self._watcher.running
        phrases = list(self._triggers.phrases) if self._triggers else []
        return {"watching": watching, "enabled": self._enabled, "triggers": phrases}

    # --- ввод текста голосом ----------------------------------------------

    @tool(routable=False, reversible=False)
    async def type_text(self, text: str = "") -> ToolResult:
        """Впечатать продиктованное в активное поле — голосовой ввод в фокус.

        Инструмент **не в каталоге модели**: его зовёт резолвер `verbatim` по
        приставке («впиши …»), отдавая весь хвост реплики дословно. Так диктовку
        не режут шаблоны и не усекает разбор — а это и было бедой первой версии.

        По умолчанию текст не набивается буквально, а **переписывается моделью**
        в аккуратное сообщение: продиктованное вслух звучит рвано, с оговорками
        и «э-э», и владелец ждёт, что ассистент причешет. Escape для буквального
        ввода — начать с «дословно»: «впиши дословно …». Enter не жмём в любом
        случае: вписать — наше дело, отправлять решает человек.

        :param text: что впечатать; весь хвост реплики после приставки.
        """
        body = text.strip()
        if not body:
            return ToolResult.failure(
                "нечего вписывать",
                speech={"ru": "Что вписать, сэр?", "en": "What should I type?"},
            )
        if sys.platform != "win32":
            return ToolResult.failure(
                "ввод текста доступен только на Windows",
                speech={"ru": "Вписывать текст я умею только на Windows.",
                        "en": "I can only type text on Windows."},
            )

        literal, body = strip_verbatim_marker(body)
        if self._rewrite and not literal and self.context.llm.available:
            body = await self._polish(body)

        try:
            sent = await asyncio.to_thread(type_text_os, body)
        except OSError as error:
            self.log.warning("Не удалось впечатать текст: %s", error)
            return ToolResult.failure(
                f"ввод текста не прошёл: {error}",
                speech={"ru": "Не получилось вписать.", "en": "I couldn't type it."},
            )
        self.log.info("Вписал %d символов", len(body))
        return ToolResult.success(
            {"typed": body, "events": sent},
            speech={"ru": ("Готово, сэр.", "Вписал.", "Готово."),
                    "en": ("Done, sir.", "Typed it.")},
        )

    async def _polish(self, body: str) -> str:
        """Переписать продиктованное в аккуратный текст. Сбой — вернуть как есть.

        Печатать нечего, если модель промолчала или упала, поэтому любой сбой —
        это буквальный ввод, а не пустое поле: продиктованное дороже красоты.
        """
        try:
            polished = await self.context.llm.ask(body, task="dialog", system=_POLISH)
        except LLMErrors as error:
            self.log.warning("Причесать текст не вышло, впишу дословно: %s", error)
            return body
        clean = polished.strip().strip("«»\"'").strip()
        if clean and clean != body:
            self.log.info("Причесал: %r -> %r", body, clean)
        return clean or body

    async def health(self) -> HealthStatus:
        """Здоровье: на своей платформе и включённый — должен и следить."""
        if sys.platform != "win32":
            return HealthStatus.degraded("только Windows")
        if self._enabled and (self._watcher is None or not self._watcher.running):
            return HealthStatus.degraded("включён, но хук не встал")
        state = "следит" if self._enabled else "выключен"
        return HealthStatus.healthy(state)
