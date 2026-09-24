"""Управление указателем мыши: перемещение, клики, колесо и перетаскивание."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import functools
import platform
import time
from collections.abc import Iterator

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool

# Флаги события мыши из WinUser.h
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_HWHEEL = 0x1000

_INPUT_MOUSE = 0
_WHEEL_DELTA = 120
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1

# Пауза между нажатием и отпусканием: слишком быстрый щелчок окна теряют.
_HOLD_PAUSE = 0.02
# Пауза между щелчками в серии, чтобы они не слиплись в один двойной.
_CLICK_PAUSE = 0.06

# Кнопки: имя -> (нажать, отпустить). Русские названия нужны для голоса.
_BUTTONS: dict[str, tuple[int, int]] = {
    "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
    "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
    "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
    "левая": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
    "правая": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
    "средняя": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
}

_BUTTON_SPEECH_RU = {"left": "левой", "right": "правой", "middle": "средней"}


class _Point(ctypes.Structure):
    """Структура POINT для GetCursorPos."""

    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MouseInput(ctypes.Structure):
    """Структура MOUSEINPUT для SendInput."""

    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouse_data", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("extra_info", ctypes.c_void_p),
    ]


class _InputUnion(ctypes.Union):
    """Объединение INPUT, из которого используется только мышь."""

    _fields_ = [("mi", _MouseInput)]


class _Input(ctypes.Structure):
    """Структура INPUT целиком."""

    _fields_ = [("type", ctypes.c_ulong), ("payload", _InputUnion)]


@functools.lru_cache(maxsize=1)
def _user32() -> ctypes.CDLL:
    """Возвращает библиотеку user32 с настроенными сигнатурами."""
    library = ctypes.WinDLL("user32")
    library.SendInput.argtypes = (ctypes.c_uint, ctypes.c_void_p, ctypes.c_int)
    library.SendInput.restype = ctypes.c_uint
    library.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
    library.SetCursorPos.restype = ctypes.c_int
    library.GetCursorPos.argtypes = (ctypes.POINTER(_Point),)
    library.GetCursorPos.restype = ctypes.c_int
    library.GetSystemMetrics.argtypes = (ctypes.c_int,)
    library.GetSystemMetrics.restype = ctypes.c_int
    return library


@functools.lru_cache(maxsize=1)
def _kernel32() -> ctypes.CDLL:
    """Возвращает библиотеку kernel32: нужна только ради GetLastError."""
    library = ctypes.WinDLL("kernel32")
    library.GetLastError.argtypes = ()
    library.GetLastError.restype = ctypes.c_ulong
    return library


def _api_error(call: str) -> OSError:
    """Собирает ошибку по коду Windows: сам API исключений не бросает."""
    code = int(_kernel32().GetLastError())
    return OSError(code, f"вызов {call} отклонён системой, код {code}")


def _is_windows() -> bool:
    """Проверяет, что мы на Windows: только там есть нужный API."""
    return platform.system() == "Windows"


def _plural_ru(number: int, one: str, few: str, many: str) -> str:
    """Выбирает русскую форму существительного под число."""
    tail_hundred = abs(number) % 100
    tail = abs(number) % 10
    if 11 <= tail_hundred <= 14:
        return many
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def _plural_en(number: int, one: str, many: str) -> str:
    """Выбирает английскую форму существительного под число."""
    return one if abs(number) == 1 else many


def _screen_size() -> tuple[int, int]:
    """Размер основного экрана в пикселях."""
    library = _user32()
    width = int(library.GetSystemMetrics(_SM_CXSCREEN))
    height = int(library.GetSystemMetrics(_SM_CYSCREEN))
    if width <= 0 or height <= 0:
        raise _api_error("GetSystemMetrics")
    return width, height


def _cursor_position() -> tuple[int, int]:
    """Текущие координаты указателя."""
    point = _Point()
    if not _user32().GetCursorPos(ctypes.byref(point)):
        raise _api_error("GetCursorPos")
    return int(point.x), int(point.y)


def _send(events: list[tuple[int, int, int, int]]) -> int:
    """Шлёт события мыши (dx, dy, данные колеса, флаги), возвращает принятые."""
    batch = (_Input * len(events))()
    for index, (dx, dy, mouse_data, flags) in enumerate(events):
        batch[index].type = _INPUT_MOUSE
        batch[index].payload.mi = _MouseInput(
            dx=dx,
            dy=dy,
            mouse_data=ctypes.c_ulong(mouse_data & 0xFFFFFFFF).value,
            flags=flags,
            time=0,
            extra_info=None,
        )
    return int(
        _user32().SendInput(len(events), ctypes.byref(batch), ctypes.sizeof(_Input))
    )


def _send_all(events: list[tuple[int, int, int, int]]) -> None:
    """Шлёт события и требует, чтобы система приняла их все до единого."""
    if _send(events) != len(events):
        raise _api_error("SendInput")


@contextlib.contextmanager
def _button_held(button: str) -> Iterator[None]:
    """Держит кнопку нажатой и отпускает её при любом исходе тела блока."""
    down, up = _BUTTONS[button]
    _send_all([(0, 0, 0, down)])
    finished = False
    try:
        yield
        finished = True
    finally:
        if finished:
            _send_all([(0, 0, 0, up)])
        else:
            # Тело уже упало: отпускаем молча, иначе кнопка залипнет на сеанс,
            # а наверх должна уйти исходная причина сбоя.
            with contextlib.suppress(OSError):
                _send_all([(0, 0, 0, up)])


def _move_absolute(x: int, y: int) -> tuple[int, int]:
    """Ставит курсор в точку экрана, обрезая координаты по его границам."""
    width, height = _screen_size()
    target_x = max(0, min(x, width - 1))
    target_y = max(0, min(y, height - 1))
    if not _user32().SetCursorPos(target_x, target_y):
        raise _api_error("SetCursorPos")
    return target_x, target_y


def _move_relative(dx: int, dy: int) -> tuple[int, int]:
    """Сдвигает курсор относительно текущего места."""
    current_x, current_y = _cursor_position()
    return _move_absolute(current_x + dx, current_y + dy)


def _click(button: str, clicks: int) -> tuple[int, int]:
    """Делает нужное число щелчков указанной кнопкой."""
    for number in range(clicks):
        if number:
            time.sleep(_CLICK_PAUSE)
        with _button_held(button):
            time.sleep(_HOLD_PAUSE)
    return _cursor_position()


def _scroll(notches: int, horizontal: bool) -> None:
    """Крутит колесо на заданное число щелчков."""
    flag = _MOUSEEVENTF_HWHEEL if horizontal else _MOUSEEVENTF_WHEEL
    _send_all([(0, 0, notches * _WHEEL_DELTA, flag)])


def _drag(x: int, y: int, button: str) -> tuple[int, int]:
    """Тянет объект из текущей точки в указанную."""
    with _button_held(button):
        time.sleep(_HOLD_PAUSE)
        target = _move_absolute(x, y)
        time.sleep(_HOLD_PAUSE)
    return target


def _normalize_button(button: str) -> str | None:
    """Приводит название кнопки к английскому виду, либо возвращает None."""
    cleaned = button.strip().lower()
    if not cleaned:
        return "left"
    if cleaned not in _BUTTONS:
        return None
    down = _BUTTONS[cleaned][0]
    for name in ("left", "right", "middle"):
        if _BUTTONS[name][0] == down:
            return name
    return None


def _unsupported() -> ToolResult:
    """Ответ для систем, где управление мышью недоступно."""
    return ToolResult.failure(
        f"Управление мышью доступно только в Windows, сейчас {platform.system()}.",
        speech={
            "ru": "Управлять мышью я умею только в Windows.",
            "en": "I can only control the mouse on Windows.",
        },
    )


def _api_failed(error: OSError) -> ToolResult:
    """Ответ, когда система отказала: нет сессии, экран заблокирован, UIPI."""
    return ToolResult.failure(
        f"Система не дала управлять мышью: {error}",
        speech={
            "ru": "Система сейчас не даёт мне управлять мышью.",
            "en": "The system will not let me control the mouse right now.",
        },
    )


def _unknown_button(button: str) -> ToolResult:
    """Ответ про кнопку, которой нет в справочнике."""
    return ToolResult.failure(
        f"Неизвестная кнопка мыши: {button}.",
        speech={
            "ru": "Такой кнопки мыши я не знаю.",
            "en": "I do not know that mouse button.",
        },
    )


class MouseSkill(Skill):
    """Двигает указатель мыши, кликает кнопками и крутит колесо."""

    meta = SkillMeta(
        name="mouse",
        description=(
            "Перемещает курсор, нажимает кнопки мыши, крутит колесо "
            "и перетаскивает."
        ),
        version="0.1.1",
        spoken=("мышь", "mouse"),
    )

    async def health(self) -> HealthStatus:
        """Проверяет, что системный API мыши доступен."""
        if not _is_windows():
            return HealthStatus.degraded(
                f"Управление мышью требует Windows, сейчас {platform.system()}."
            )
        try:
            await asyncio.to_thread(_screen_size)
            await asyncio.to_thread(_cursor_position)
        except OSError as error:
            return HealthStatus.degraded(f"Не удалось обратиться к user32: {error}")
        return HealthStatus.healthy()

    # Без фраз и без координат по умолчанию: «передвинь мышь» без чисел
    # отправляло курсор в левый верхний угол (x=0, y=0). Координаты называет модель.
    @tool(reversible=True)
    async def move_cursor(self, x: int, y: int, relative: bool = False) -> ToolResult:
        """Перемещает указатель мыши в точку экрана или на смещение от текущей.

        :param x: координата по горизонтали или сдвиг вправо, если relative.
        :param y: координата по вертикали или сдвиг вниз, если relative.
        :param relative: считать координаты смещением от текущего места.
        """
        if not _is_windows():
            return _unsupported()
        try:
            previous = await asyncio.to_thread(_cursor_position)
            if relative:
                position = await asyncio.to_thread(_move_relative, x, y)
            else:
                position = await asyncio.to_thread(_move_absolute, x, y)
        except OSError as error:
            return _api_failed(error)
        return ToolResult.success(
            {
                "x": position[0],
                "y": position[1],
                "previous": {"x": previous[0], "y": previous[1]},
            },
            speech={
                "ru": (
                    f"Курсор по горизонтали {position[0]}, "
                    f"по вертикали {position[1]}."
                ),
                "en": (
                    f"The cursor is {position[0]} across and {position[1]} down."
                ),
            },
        )

    @tool(
        phrases=["кликни мышью", "нажми кнопку мыши", "щёлкни мышкой"],
        reversible=False,
    )
    async def click(self, button: str = "left", clicks: int = 1) -> ToolResult:
        """Нажимает кнопку мыши в текущей точке указателя.

        :param button: кнопка — left, right или middle.
        :param clicks: сколько щелчков сделать, от одного до трёх.
        """
        if not _is_windows():
            return _unsupported()
        name = _normalize_button(button)
        if name is None:
            return _unknown_button(button)
        count = max(1, min(clicks, 3))
        try:
            position = await asyncio.to_thread(_click, name, count)
        except OSError as error:
            return _api_failed(error)
        times_ru = _plural_ru(count, "раз", "раза", "раз")
        times_en = _plural_en(count, "time", "times")
        return ToolResult.success(
            {"button": name, "clicks": count, "x": position[0], "y": position[1]},
            speech={
                "ru": f"Нажал {_BUTTON_SPEECH_RU[name]} кнопкой {count} {times_ru}.",
                "en": f"Clicked the {name} button {count} {times_en}.",
            },
        )

    # «Проскролль вверх» раньше вело сюда же с notches=-3, то есть крутило вниз.
    # Направления — отдельными командами ниже.
    @tool(reversible=True)
    async def scroll(self, notches: int = -3, horizontal: bool = False) -> ToolResult:
        """Крутит колесо мыши: плюс — вверх или вправо, минус — вниз или влево.

        :param notches: число щелчков колеса со знаком направления.
        :param horizontal: крутить горизонтально вместо вертикали.
        """
        if not _is_windows():
            return _unsupported()
        if notches == 0:
            return ToolResult.failure(
                "Нужно ненулевое число щелчков колеса.",
                speech={
                    "ru": "Скажите, на сколько прокрутить.",
                    "en": "Tell me how far to scroll.",
                },
            )
        steps = max(-30, min(notches, 30))
        try:
            await asyncio.to_thread(_scroll, steps, horizontal)
        except OSError as error:
            return _api_failed(error)
        amount = abs(steps)
        notch_ru = _plural_ru(amount, "щелчок", "щелчка", "щелчков")
        notch_en = _plural_en(amount, "notch", "notches")
        return ToolResult.success(
            {"notches": steps, "horizontal": horizontal},
            speech={
                "ru": f"Прокрутил на {amount} {notch_ru}.",
                "en": f"Scrolled {amount} {notch_en}.",
            },
        )

    @tool(phrases=["проскролль вниз", "прокрути вниз", "листай вниз"], routable=False, reversible=True)
    async def scroll_down(self) -> ToolResult:
        """Прокрутить вниз на три щелчка колеса."""
        return await self.scroll(-3)

    @tool(phrases=["проскролль вверх", "прокрути вверх", "листай вверх"], routable=False, reversible=True)
    async def scroll_up(self) -> ToolResult:
        """Прокрутить вверх на три щелчка колеса."""
        return await self.scroll(3)

    # Без фраз и без координат по умолчанию: «перетащи мышью» без чисел тянуло
    # с зажатой кнопкой в угол экрана — можно утащить файл или окно.
    @tool(reversible=False)
    async def drag(self, x: int, y: int, button: str = "left") -> ToolResult:
        """Тянет указатель с зажатой кнопкой из текущей точки в заданную.

        :param x: конечная координата по горизонтали.
        :param y: конечная координата по вертикали.
        :param button: какую кнопку держать — left, right или middle.
        """
        if not _is_windows():
            return _unsupported()
        name = _normalize_button(button)
        if name is None:
            return _unknown_button(button)
        try:
            start = await asyncio.to_thread(_cursor_position)
            target = await asyncio.to_thread(_drag, x, y, name)
        except OSError as error:
            return _api_failed(error)
        return ToolResult.success(
            {
                "button": name,
                "from": {"x": start[0], "y": start[1]},
                "to": {"x": target[0], "y": target[1]},
            },
            speech={
                "ru": (
                    f"Перетащил по горизонтали на {target[0]}, "
                    f"по вертикали на {target[1]}."
                ),
                "en": f"Dragged to {target[0]} across and {target[1]} down.",
            },
        )

    @tool(phrases=["где курсор", "какие координаты мыши"], reversible=True, routable=False)
    async def where(self) -> ToolResult:
        """Сообщает текущие координаты указателя и размер экрана."""
        if not _is_windows():
            return _unsupported()
        try:
            position = await asyncio.to_thread(_cursor_position)
            width, height = await asyncio.to_thread(_screen_size)
        except OSError as error:
            return _api_failed(error)
        return ToolResult.success(
            {"x": position[0], "y": position[1], "width": width, "height": height},
            speech={
                "ru": (
                    f"Курсор по горизонтали {position[0]}, "
                    f"по вертикали {position[1]}."
                ),
                "en": (
                    f"The cursor is {position[0]} across and {position[1]} down."
                ),
            },
        )
