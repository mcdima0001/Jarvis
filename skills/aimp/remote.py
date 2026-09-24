r"""Разговор с AIMP через окно, которое он держит для Winamp.

Замер 24.09.2026 на машине владельца: у запущенного AIMP среди двух десятков
окон есть `AIMP2_RemoteInfo` и **`Winamp v1.x`** — он эмулирует старый Winamp,
чей набор сообщений описан двадцать лет назад и с тех пор не менялся. Взят
именно он: сообщения простые, а главное — **окно невидимое**, то есть работает
и когда плеер свёрнут в трей. Мультимедийные кнопки так не умеют: они шлются
видимым окнам, а музыку как раз слушают свёрнутой.

Что замер показал (AIMP играл «Xtreem - Covet»):

| спросили | ответ |
|---|---|
| заголовок окна | `11. Xtreem - Covet - Winamp` |
| состояние | 1 — играет |
| позиция | 29600 мс, длина 120 с |
| номер в списке | 10 из 1423 |
| следующий и предыдущий трек | сработали оба |
| прыжок на 33-й номер | заиграл ровно он |

Заголовок — это и есть «что играет»: номер, исполнитель и название, и читается
он у **невидимого** окна, то есть из трея тоже.
"""

from __future__ import annotations

import ctypes
import logging
import re
import sys
from ctypes import wintypes
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Класс окна, которое AIMP держит для совместимости с Winamp.
WINDOW = "Winamp v1.x"

WM_COMMAND = 0x0111
WM_USER = 0x0400
SMTO_ABORTIFHUNG = 0x0002

#: Кнопки Winamp: их номера — это пункты его меню, и AIMP отвечает на те же.
PREVIOUS, PLAY, PAUSE, STOP, NEXT = 40044, 40045, 40046, 40047, 40048

#: Запросы. Второе число — «о чём спрашиваем», первое уходит в wParam.
ASK_STATE = 104        # 1 играет, 3 пауза, 0 стоит
ASK_POSITION = 105     # мс; с wParam=1 — длина трека в секундах
ASK_LENGTH = 124       # сколько треков в открытом списке
ASK_NUMBER = 125       # какой из них играет, с нуля
SET_NUMBER = 121       # переключиться на трек по номеру

#: Сколько ждать ответа. Плеер отвечает мгновенно, но зависший не должен
#: задерживать реплику: голос важнее.
WAIT_MS = 400

#: Заголовок приходит как «11. Исполнитель - Название - Winamp».
_TITLE = re.compile(r"^\s*(?:\d+\.\s*)?(?P<said>.*?)(?:\s+-\s+Winamp)?\s*$")


@dataclass(frozen=True)
class Playing:
    """Что AIMP играет прямо сейчас."""

    said: str
    number: int
    total: int
    state: int
    position_s: float
    length_s: float

    @property
    def playing(self) -> bool:
        return self.state == 1

    @property
    def paused(self) -> bool:
        return self.state == 3


def _user32() -> ctypes.WinDLL:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
    ]
    return user32


def window() -> int:
    """Окно AIMP или ноль, если плеер не запущен."""
    if sys.platform != "win32":
        return 0
    return int(_user32().FindWindowW(WINDOW, None) or 0)


def running() -> bool:
    """Запущен ли AIMP. Стоит доли миллисекунды — поиск окна по классу."""
    return window() != 0


def _ask(handle: int, what: int, word: int = 0) -> int | None:
    user32 = _user32()
    out = ctypes.c_size_t()
    ok = user32.SendMessageTimeoutW(
        handle, WM_USER, ctypes.c_void_p(word), ctypes.c_void_p(what),
        SMTO_ABORTIFHUNG, WAIT_MS, ctypes.byref(out),
    )
    return int(out.value) if ok else None


def press(command: int, handle: int = 0) -> bool:
    """Нажать кнопку плеера. Ложь — плеера нет или он не ответил."""
    handle = handle or window()
    if not handle:
        return False
    user32 = _user32()
    out = ctypes.c_size_t()
    return bool(user32.SendMessageTimeoutW(
        handle, WM_COMMAND, ctypes.c_void_p(command), None,
        SMTO_ABORTIFHUNG, WAIT_MS, ctypes.byref(out),
    ))


def said_from_title(title: str) -> str:
    """Вытащить «Исполнитель - Название» из заголовка окна.

    Когда плеер ничего не играет, в заголовке остаётся одно слово «Winamp» —
    это не название трека, а пустота, и отдавать её как ответ нельзя.
    """
    match = _TITLE.match(title.strip())
    said = match.group("said").strip() if match else title.strip()
    return "" if said == "Winamp" else said


def playing() -> Playing | None:
    """Что играет. ``None`` — AIMP не запущен."""
    handle = window()
    if not handle:
        return None
    user32 = _user32()
    buffer = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(handle, buffer, 512)
    position = _ask(handle, ASK_POSITION) or 0
    return Playing(
        said=said_from_title(buffer.value),
        number=_ask(handle, ASK_NUMBER) if _ask(handle, ASK_NUMBER) is not None else -1,
        total=_ask(handle, ASK_LENGTH) or 0,
        state=_ask(handle, ASK_STATE) or 0,
        position_s=position / 1000.0,
        length_s=float(_ask(handle, ASK_POSITION, 1) or 0),
    )


def play_number(number: int) -> bool:
    """Включить трек по его номеру в открытом списке.

    Именно номером, а не файлом: так в фонотеку владельца ничего не
    добавляется. Замер показал прыжок на нужный трек без единой правки списка.
    """
    handle = window()
    if not handle or number < 0:
        return False
    if _ask(handle, SET_NUMBER, number) is None:
        return False
    return press(PLAY, handle)
