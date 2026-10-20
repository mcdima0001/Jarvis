"""Всплывашка справа внизу — тихое уведомление в цветах панели.

Просьба владельца (20.10.2026): «протокол такой-то запущен» — не голосом, а
окошком. Голосом это не годится там, где протокол и нужен больше всего: в
конференции реплика прозвучала бы на всех участников.

Окно своё, как и меню трея (`popup.py`): без рамки, поверх всех, **фокус не
забирает** — уведомление, отнимающее клавиатуру посреди набора, хуже, чем никакое.
Появляется плавно, держится несколько секунд и гаснет; щелчок убирает сразу.
Новое уведомление заменяет текущее, а не встаёт в очередь: «запущен» и тут же
«свёрнут» важны оба, но последнее — правда.

Размеры, место и прозрачность по времени — чистые функции в `menu.py`, их
проверяют тесты на любой машине. Здесь рисование и окно, на потоке значка.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from collections.abc import Mapping
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .menu import (
    KIND_COLORS,
    PALETTE,
    POPUP_GAP,
    READY,
    ToastLayout,
    toast_alpha,
    toast_layout,
    toast_position,
)
from .popup import (
    _NULL_PEN,
    _PAINTSTRUCT,
    _SRCCOPY,
    _TRANSPARENT,
    _WNDCLASSW,
    _WNDPROC,
    Painter,
    _round_corners,
    load_libraries,
)

logger = logging.getLogger(__name__)

_WS_POPUP = 0x80000000
_WS_EX_TOPMOST, _WS_EX_TOOLWINDOW, _WS_EX_LAYERED, _WS_EX_NOACTIVATE = 0x8, 0x80, 0x80000, 0x08000000
_SW_SHOWNOACTIVATE = 4
_LWA_ALPHA = 0x2
_HWND_TOPMOST = -1
_SWP_NOACTIVATE, _SWP_NOSIZE, _SWP_NOMOVE = 0x10, 0x1, 0x2
_WM_PAINT, _WM_ERASEBKGND, _WM_TIMER, _WM_LBUTTONUP, _WM_RBUTTONUP, _WM_MOUSEACTIVATE = (
    0xF, 0x14, 0x113, 0x202, 0x205, 0x21,
)
_MA_NOACTIVATE = 3
_MONITOR_DEFAULTTOPRIMARY = 1
_IMAGE_ICON, _LR_LOADFROMFILE, _DI_NORMAL = 1, 0x10, 0x3
_TIMER_ID = 7
_TICK_MS = 30


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class ToastPainter(Painter):
    """Рисует всплывашку: полоска слева, шапка как у меню, текст под ней."""

    def __init__(self, user32: Any, gdi32: Any, scale: float) -> None:
        super().__init__(user32, gdi32, scale)
        self._fonts["text"] = self._font(15, 600, "Segoe UI Semibold")

    def paint_toast(self, hdc: int, layout: ToastLayout, text: str, detail: str, kind: str, icon: int | None) -> None:
        """Нарисовать целиком — сначала в память, потом разом: без мерцания."""
        gdi, px = self._gdi, self.px
        width, height = layout.width, layout.height
        memory = gdi.CreateCompatibleDC(hdc)
        bitmap = gdi.CreateCompatibleBitmap(hdc, width, height)
        previous = gdi.SelectObject(memory, bitmap)
        try:
            gdi.SetBkMode(memory, _TRANSPARENT)
            self._fill(memory, (0, 0, width, height), PALETTE["background"])
            color = KIND_COLORS.get(kind, PALETTE["accent"])
            # Полоска слева — как подсветка пункта в меню и в панели.
            self._fill(memory, (0, 0, px(3), height), color)

            size = px(20)
            if icon:
                self._user.DrawIconEx(memory, px(16), px(11), icon, size, size, 0, None, _DI_NORMAL)
            self._text(memory, "J.A.R.V.I.S.", (px(44), px(9), width - px(12), px(33)), "title", PALETTE["accent"], px(3))
            self._fill(memory, (px(14), px(40), width - px(14), px(41)), PALETTE["line"])

            dot = px(8)
            top = px(48)
            dot_top = top + (px(22) - dot) // 2
            gdi.SelectObject(memory, gdi.GetStockObject(_NULL_PEN))
            gdi.SelectObject(memory, self._brush(color))
            gdi.Ellipse(memory, px(18), dot_top, px(18) + dot + 1, dot_top + dot + 1)
            self._text(memory, text, (px(36), top, width - px(14), top + px(22)), "text", PALETTE["text"])
            if detail:
                self._text(memory, detail, (px(36), top + px(23), width - px(14), top + px(41)), "status", PALETTE["muted"])

            gdi.BitBlt(hdc, 0, 0, width, height, memory, 0, 0, _SRCCOPY)
        finally:
            gdi.SelectObject(memory, previous)
            gdi.DeleteObject(bitmap)
            gdi.DeleteDC(memory)


class Toast:
    """Всплывашка. Живёт на потоке значка и зовётся только оттуда.

    :param icons: файлы значков по состояниям — реактор в шапке.
    """

    def __init__(self, icons: Mapping[str, Path]) -> None:
        self._icon_files = dict(icons)
        self._user32: Any = None
        self._gdi32: Any = None
        self._instance: Any = None
        self._class = f"JarvisToast{id(self)}"
        self._proc = _WNDPROC(self._window_proc)  # ссылку держим сами, иначе соберёт GC
        self._hwnd: int | None = None
        self._painter: ToastPainter | None = None
        self._layout: ToastLayout | None = None
        self._icon: int | None = None
        self._icon_size = 0
        self._text = self._detail = ""
        self._kind = "info"
        self._shown_at = 0.0

    def show(self, text: str, detail: str = "", kind: str = "info") -> bool:
        """Показать уведомление, заменив текущее. ``False`` — не вышло."""
        if sys.platform != "win32":
            return False
        try:
            self._prepare()
            self.close()
            user32 = self._user32
            work, scale = self._primary_work()
            layout = toast_layout(scale, detail=bool(detail))
            x, y = toast_position(work, (layout.width, layout.height), round(POPUP_GAP * 2 * scale))
            self._painter = ToastPainter(user32, self._gdi32, scale)
            self._layout, self._text, self._detail, self._kind = layout, text, detail, kind
            self._icon = self._load_icon(self._painter.px(20))
            hwnd = user32.CreateWindowExW(
                _WS_EX_TOPMOST | _WS_EX_TOOLWINDOW | _WS_EX_LAYERED | _WS_EX_NOACTIVATE,
                self._class, "Jarvis", _WS_POPUP, x, y, layout.width, layout.height,
                None, None, self._instance, None,
            )
            if not hwnd:
                raise OSError(f"окно всплывашки не создано ({ctypes.get_last_error()})")
            self._hwnd = hwnd
            user32.SetLayeredWindowAttributes(hwnd, 0, 0, _LWA_ALPHA)
            _round_corners(hwnd)
            user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
            user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, _SWP_NOACTIVATE | _SWP_NOSIZE | _SWP_NOMOVE)
            self._shown_at = time.monotonic()
            user32.SetTimer(hwnd, _TIMER_ID, _TICK_MS, None)
            return True
        except Exception:  # noqa: BLE001 — уведомление не стоит падения значка
            logger.exception("Всплывашка не показалась: %s", text)
            self.close()
            return False

    def close(self) -> None:
        """Убрать, если показана."""
        hwnd, self._hwnd = self._hwnd, None
        if hwnd and self._user32 is not None:
            self._user32.KillTimer(hwnd, _TIMER_ID)
            self._user32.DestroyWindow(hwnd)
        if self._painter is not None:
            self._painter.close()
            self._painter = None

    def dispose(self) -> None:
        """Убрать и отдать системе класс окна и значок."""
        self.close()
        if self._user32 is None:
            return
        if self._icon:
            self._user32.DestroyIcon(self._icon)
            self._icon = None
        self._user32.UnregisterClassW(self._class, self._instance)

    # --- внутреннее ----------------------------------------------------------

    def _prepare(self) -> None:
        if self._user32 is not None:
            return
        user32, gdi32 = load_libraries()
        handle = wintypes.HANDLE
        user32.SetLayeredWindowAttributes.argtypes = [handle, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD]
        user32.SetWindowPos.argtypes = [handle, handle, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.MonitorFromPoint.restype = handle
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.GetMonitorInfoW.argtypes = [handle, ctypes.POINTER(_MONITORINFO)]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetModuleHandleW.restype = wintypes.HANDLE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        instance = kernel32.GetModuleHandleW(None)
        window_class = _WNDCLASSW()
        window_class.lpfnWndProc = self._proc
        window_class.hInstance = instance
        window_class.lpszClassName = self._class
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            raise OSError(f"класс окна всплывашки не зарегистрирован ({ctypes.get_last_error()})")
        self._user32, self._gdi32, self._instance = user32, gdi32, instance

    def _primary_work(self) -> tuple[tuple[int, int, int, int], float]:
        """Рабочая область главного монитора и его масштаб: там и часы."""
        from .popup import StyledMenu

        user32 = self._user32
        monitor = user32.MonitorFromPoint(wintypes.POINT(0, 0), _MONITOR_DEFAULTTOPRIMARY)
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise OSError("главный монитор не нашёлся")
        work = info.rcWork
        return (work.left, work.top, work.right, work.bottom), StyledMenu._dpi(monitor) / 96

    def _load_icon(self, size: int) -> int | None:
        if self._icon and self._icon_size == size:
            return self._icon
        if self._icon:
            self._user32.DestroyIcon(self._icon)
            self._icon = None
        path = self._icon_files.get(READY)
        if path is None:
            return None
        icon = self._user32.LoadImageW(None, str(path), _IMAGE_ICON, size, size, _LR_LOADFROMFILE)
        self._icon_size = size
        return int(icon) if icon else None

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        """Оконная процедура. Падать ей нельзя: исключение уйдёт в ОС."""
        user32 = self._user32
        try:
            if hwnd == self._hwnd and self._layout is not None:
                if message == _WM_PAINT:
                    paint = _PAINTSTRUCT()
                    hdc = user32.BeginPaint(hwnd, ctypes.byref(paint))
                    try:
                        if self._painter is not None:
                            self._painter.paint_toast(
                                hdc, self._layout, self._text, self._detail, self._kind, self._icon
                            )
                    finally:
                        user32.EndPaint(hwnd, ctypes.byref(paint))
                    return 0
                if message == _WM_ERASEBKGND:
                    return 1
                if message == _WM_MOUSEACTIVATE:
                    # Щелчок по уведомлению не должен уводить фокус из окна, где печатали.
                    return _MA_NOACTIVATE
                if message in (_WM_LBUTTONUP, _WM_RBUTTONUP):
                    self.close()
                    return 0
                if message == _WM_TIMER:
                    alpha = toast_alpha(time.monotonic() - self._shown_at)
                    if alpha is None:
                        self.close()
                    else:
                        user32.SetLayeredWindowAttributes(hwnd, 0, alpha, _LWA_ALPHA)
                    return 0
        except Exception:  # noqa: BLE001 — процедура окна не имеет права падать
            logger.exception("Всплывашка: сбой в обработке сообщения %#x", message)
        return int(user32.DefWindowProcW(hwnd, message, wparam, lparam))
