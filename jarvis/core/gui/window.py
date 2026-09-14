"""Окно панели в Windows: найти, узнать, где стоит, поставить на место.

Первая версия брала положение у страницы (`window.screenX`) и открывала окно
флагами `--window-size` и `--window-position`. Положение сохранялось верно, но
Edge, уже запущенный как обычный браузер, **молча игнорирует эти флаги** у
нового окна — они действуют только при старте самого браузера. Панель
открывалась где попало (живой запуск 14.09.2026). Поэтому и узнаём, и ставим
окно сами, через WinAPI.

**Всё в физических пикселях и в потоке, осведомлённом о масштабе каждого
монитора.** Иначе на экране 125% Windows отдаёт и принимает координаты,
пересчитанные под «логический» дисплей, а у разных мониторов масштаб разный —
окно ставилось бы не туда и не того размера.
"""

from __future__ import annotations

import ctypes
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from ctypes import wintypes
from typing import Any

#: Заголовок окна панели — это `<title>` страницы.
TITLE = "J.A.R.V.I.S."

_SWP_NOZORDER, _SWP_NOACTIVATE = 0x0004, 0x0010
_PER_MONITOR_AWARE_V2 = -4

Geometry = tuple[int, int, int, int]


def _user32() -> Any:
    if sys.platform != "win32":
        return None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    handle = ctypes.c_void_p
    user32.GetWindowTextLengthW.argtypes = [handle]
    user32.GetWindowTextW.argtypes = [handle, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [handle]
    user32.IsIconic.argtypes = [handle]
    user32.IsZoomed.argtypes = [handle]
    user32.GetWindowRect.argtypes = [handle, ctypes.POINTER(wintypes.RECT)]
    user32.SetWindowPos.argtypes = [
        handle, handle, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    return user32


@contextmanager
def _dpi_aware(user32: Any) -> Iterator[None]:
    """На время вызова считать этот поток осведомлённым о масштабе каждого монитора."""
    setter = getattr(user32, "SetThreadDpiAwarenessContext", None)
    previous = None
    if setter is not None:
        setter.restype = ctypes.c_void_p
        setter.argtypes = [ctypes.c_void_p]
        previous = setter(ctypes.c_void_p(_PER_MONITOR_AWARE_V2))
    try:
        yield
    finally:
        if setter is not None and previous:
            setter(ctypes.c_void_p(previous))


def panel_windows() -> list[int]:
    """Видимые окна с заголовком панели."""
    user32 = _user32()
    if user32 is None:
        return []
    found: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]

    def visit(hwnd: int, _: int) -> bool:
        length = user32.GetWindowTextLengthW(hwnd)
        if length == len(TITLE) and user32.IsWindowVisible(hwnd):
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value == TITLE:
                found.append(int(hwnd))
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return found


def window_rect(hwnd: int) -> Geometry | None:
    """Где стоит окно: x, y, ширина, высота. ``None`` — свёрнуто или развёрнуто.

    Свёрнутое окно Windows «уносит» в −32000, а развёрнутое занимает весь экран:
    запомнить такое значило бы в следующий раз открыть окно за краем или без рамки.
    """
    user32 = _user32()
    if user32 is None:
        return None
    with _dpi_aware(user32):
        if user32.IsIconic(hwnd) or user32.IsZoomed(hwnd):
            return None
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def panel_rect() -> Geometry | None:
    """Где стоит окно панели, если оно открыто."""
    for hwnd in panel_windows():
        return window_rect(hwnd)
    return None


def virtual_screen() -> Geometry | None:
    """Все мониторы вместе, в физических пикселях."""
    user32 = _user32()
    if user32 is None:
        return None
    with _dpi_aware(user32):
        # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN
        left, top, width, height = (int(user32.GetSystemMetrics(index)) for index in (76, 77, 78, 79))
    return left, top, width, height


def move_window(hwnd: int, geometry: Geometry) -> None:
    """Поставить окно на место, не отбирая фокус."""
    user32 = _user32()
    if user32 is None:
        return
    x, y, width, height = geometry
    with _dpi_aware(user32):
        user32.SetWindowPos(hwnd, None, x, y, width, height, _SWP_NOZORDER | _SWP_NOACTIVATE)


def place_new_window(
    saved: Geometry,
    before: set[int],
    *,
    timeout: float = 10.0,
    settle: float = 1.5,
    find: Callable[[], list[int]] = panel_windows,
    measure: Callable[[int], Geometry | None] = window_rect,
    place: Callable[[int, Geometry], None] = move_window,
    screen: Callable[[], Geometry | None] = virtual_screen,
    fits: Callable[[Geometry, Geometry], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Дождаться нового окна панели и поставить его туда, где оно было.

    После первой постановки ещё `settle` секунд сверяем: Edge, догрузив
    страницу, иногда сам двигает окно — тогда ставим заново.

    :param before: окна панели, открытые до запуска, — их не трогаем.
    :return: поставили ли окно.
    """
    if fits is None:
        from jarvis.core.tray.session import fits_screen

        fits = fits_screen
    area = screen()
    if area is not None and not fits(saved, area):
        return False  # сохранено на отключённом мониторе — пусть стоит, где открылось

    deadline = clock() + timeout
    fresh: list[int] = []
    while clock() < deadline:
        fresh = [hwnd for hwnd in find() if hwnd not in before]
        if fresh:
            break
        sleep(0.1)
    if not fresh:
        return False

    hwnd = fresh[0]
    place(hwnd, saved)
    settled = clock() + settle
    while clock() < settled:
        sleep(0.1)
        current = measure(hwnd)
        if current is not None and current != saved:
            place(hwnd, saved)
    return True
