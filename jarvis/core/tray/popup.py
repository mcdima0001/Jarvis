"""Своё меню значка в трее — в цветах панели, а не системное.

Просьба владельца (14.09.2026): «как у Steam, у них полностью своя менюшка».
Системное меню красится только в светлое или тёмное, шрифт и отступы у него
чужие. Поэтому меню — своё окно: без рамки, поверх всех, рисунок через GDI.
Скругление и тень даёт Windows 11 (DWM), на Windows 10 углы прямые.

Раскладка, попадание мыши, переход стрелками и место на экране — чистые
функции в `menu.py`, их проверяют тесты на любой машине. Здесь только
рисование и сообщения окна. Окно не создалось — `TrayIcon` показывает
системное меню, как раньше: остаться без меню нельзя, в нём «Выйти».
"""

from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable, Mapping
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .menu import (
    PALETTE,
    POPUP_GAP,
    READY,
    STATE_COLORS,
    STATE_WORDS,
    MenuItem,
    PopupLayout,
    popup_layout,
    popup_position,
    row_at,
    step_row,
)

logger = logging.getLogger(__name__)

_WM_DESTROY, _WM_ACTIVATE, _WM_PAINT, _WM_CLOSE, _WM_ERASEBKGND = 0x2, 0x6, 0xF, 0x10, 0x14
_WM_KEYDOWN, _WM_TIMER = 0x100, 0x113
_WM_MOUSEMOVE, _WM_LBUTTONUP, _WM_RBUTTONUP, _WM_MOUSELEAVE = 0x200, 0x202, 0x205, 0x2A3
_VK_RETURN, _VK_ESCAPE, _VK_UP, _VK_DOWN = 0x0D, 0x1B, 0x26, 0x28
_WS_POPUP, _WS_EX_TOPMOST, _WS_EX_TOOLWINDOW = 0x80000000, 0x8, 0x80
_CS_DROPSHADOW = 0x20000
_SW_HIDE, _SW_SHOW = 0, 5
_TME_LEAVE = 0x2
_DI_NORMAL = 0x3
_DT_SINGLELINE, _DT_VCENTER, _DT_NOPREFIX, _DT_END_ELLIPSIS = 0x20, 0x4, 0x800, 0x8000
_TRANSPARENT = 1
_SRCCOPY = 0x00CC0020
_NULL_PEN = 8
_DEFAULT_CHARSET, _CLEARTYPE_QUALITY = 1, 5
_DWMWA_WINDOW_CORNER_PREFERENCE, _DWMWCP_ROUND, _DWMWA_BORDER_COLOR = 33, 2, 34
_IDC_ARROW = 32512
_IMAGE_ICON, _LR_LOADFROMFILE = 1, 0x10
_TIMER_ID = 1
#: Как часто проверять, не ушёл ли фокус к другому окну, мс.
_WATCH_MS = 120

_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


class _TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("hwndTrack", wintypes.HWND),
        ("dwHoverTime", wintypes.DWORD),
    ]


def colorref(color: str) -> int:
    """«#rrggbb» в COLORREF WinAPI, где байты идут наоборот: 0x00bbggrr."""
    red, green, blue = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    return red | (green << 8) | (blue << 16)


def load_libraries() -> tuple[Any, Any]:
    """user32 и gdi32 с объявленными типами: на 64 битах без них режутся дескрипторы."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    handle = wintypes.HANDLE
    rect = ctypes.POINTER(wintypes.RECT)
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, handle]
    user32.CreateWindowExW.restype = handle
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, handle, handle, handle, ctypes.c_void_p,
    ]
    user32.DefWindowProcW.restype = _LRESULT
    user32.DefWindowProcW.argtypes = [handle, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DestroyWindow.argtypes = [handle]
    user32.ShowWindow.argtypes = [handle, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [handle]
    user32.GetForegroundWindow.restype = handle
    user32.PostMessageW.argtypes = [handle, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.InvalidateRect.argtypes = [handle, ctypes.c_void_p, wintypes.BOOL]
    user32.BeginPaint.restype = handle
    user32.BeginPaint.argtypes = [handle, ctypes.POINTER(_PAINTSTRUCT)]
    user32.EndPaint.argtypes = [handle, ctypes.POINTER(_PAINTSTRUCT)]
    user32.FillRect.argtypes = [handle, rect, handle]
    user32.DrawTextW.argtypes = [handle, wintypes.LPCWSTR, ctypes.c_int, rect, wintypes.UINT]
    user32.DrawIconEx.argtypes = [
        handle, ctypes.c_int, ctypes.c_int, handle, ctypes.c_int, ctypes.c_int, wintypes.UINT, handle, wintypes.UINT,
    ]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.SetTimer.argtypes = [handle, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
    user32.KillTimer.argtypes = [handle, ctypes.c_size_t]
    user32.TrackMouseEvent.argtypes = [ctypes.POINTER(_TRACKMOUSEEVENT)]
    user32.LoadCursorW.restype = handle
    user32.LoadCursorW.argtypes = [handle, ctypes.c_void_p]
    user32.LoadImageW.restype = handle
    user32.LoadImageW.argtypes = [handle, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.DestroyIcon.argtypes = [handle]
    gdi32.CreateCompatibleDC.restype = handle
    gdi32.CreateCompatibleDC.argtypes = [handle]
    gdi32.CreateCompatibleBitmap.restype = handle
    gdi32.CreateCompatibleBitmap.argtypes = [handle, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = handle
    gdi32.SelectObject.argtypes = [handle, handle]
    gdi32.DeleteObject.argtypes = [handle]
    gdi32.DeleteDC.argtypes = [handle]
    gdi32.BitBlt.argtypes = [
        handle, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, handle, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
    ]
    gdi32.CreateSolidBrush.restype = handle
    gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
    gdi32.CreateFontW.restype = handle
    gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]
    gdi32.SetTextColor.argtypes = [handle, wintypes.DWORD]
    gdi32.SetBkMode.argtypes = [handle, ctypes.c_int]
    gdi32.SetTextCharacterExtra.argtypes = [handle, ctypes.c_int]
    gdi32.Ellipse.argtypes = [handle, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    gdi32.GetStockObject.restype = handle
    gdi32.GetStockObject.argtypes = [ctypes.c_int]
    return user32, gdi32


class Painter:
    """Рисует меню в заданный контекст. Отдельно от окна, чтобы вид можно было
    отрисовать в картинку и посмотреть, не открывая меню на экране.
    """

    def __init__(self, user32: Any, gdi32: Any, scale: float) -> None:
        self._user, self._gdi = user32, gdi32
        self.scale = scale
        self._brushes: dict[str, int] = {}
        self._fonts = {
            "title": self._font(12, 600, "Segoe UI Semibold"),
            "status": self._font(12, 400, "Segoe UI"),
            "item": self._font(14, 400, "Segoe UI"),
        }

    def px(self, value: float) -> int:
        """Точки при масштабе 100% в пиксели этого монитора."""
        return round(value * self.scale)

    def _font(self, size: int, weight: int, face: str) -> int:
        return int(self._gdi.CreateFontW(
            -self.px(size), 0, 0, 0, weight, 0, 0, 0, _DEFAULT_CHARSET, 0, 0, _CLEARTYPE_QUALITY, 0, face,
        ))

    def _brush(self, color: str) -> int:
        if color not in self._brushes:
            self._brushes[color] = int(self._gdi.CreateSolidBrush(colorref(color)))
        return self._brushes[color]

    def _fill(self, hdc: int, rect: tuple[int, int, int, int], color: str) -> None:
        area = wintypes.RECT(*rect)
        self._user.FillRect(hdc, ctypes.byref(area), self._brush(color))

    def _text(self, hdc: int, text: str, rect: tuple[int, int, int, int], font: str, color: str, spacing: int = 0) -> None:
        self._gdi.SelectObject(hdc, self._fonts[font])
        self._gdi.SetTextColor(hdc, colorref(color))
        self._gdi.SetTextCharacterExtra(hdc, spacing)
        area = wintypes.RECT(*rect)
        flags = _DT_SINGLELINE | _DT_VCENTER | _DT_NOPREFIX | _DT_END_ELLIPSIS
        self._user.DrawTextW(hdc, text, -1, ctypes.byref(area), flags)

    def paint(self, hdc: int, layout: PopupLayout, state: str, hover: int | None, icon: int | None) -> None:
        """Нарисовать меню целиком — сначала в память, потом разом: без мерцания."""
        gdi, px = self._gdi, self.px
        width, height = layout.width, layout.height
        memory = gdi.CreateCompatibleDC(hdc)
        bitmap = gdi.CreateCompatibleBitmap(hdc, width, height)
        previous = gdi.SelectObject(memory, bitmap)
        try:
            gdi.SetBkMode(memory, _TRANSPARENT)
            self._fill(memory, (0, 0, width, height), PALETTE["background"])

            # Шапка: реактор, имя и точка состояния — как верх панели.
            header = layout.header
            size = px(24)
            if icon:
                self._user.DrawIconEx(memory, px(16), (header - size) // 2, icon, size, size, 0, None, _DI_NORMAL)
            left = px(52)
            self._text(memory, "J.A.R.V.I.S.", (left, px(10), width - px(12), px(30)), "title", PALETTE["accent"], px(3))
            dot = px(7)
            dot_top = px(33) + (px(16) - dot) // 2
            gdi.SelectObject(memory, gdi.GetStockObject(_NULL_PEN))
            gdi.SelectObject(memory, self._brush(STATE_COLORS.get(state, PALETTE["muted"])))
            gdi.Ellipse(memory, left, dot_top, left + dot + 1, dot_top + dot + 1)
            words = STATE_WORDS.get(state, state)
            self._text(memory, words, (left + dot + px(7), px(33), width - px(12), px(49)), "status", PALETTE["muted"])
            self._fill(memory, (px(12), header - 1, width - px(12), header), PALETTE["line"])

            for index, row in enumerate(layout.rows):
                if row.item is None:
                    middle = row.top + row.height // 2
                    self._fill(memory, (px(14), middle, width - px(14), middle + 1), PALETTE["line"])
                    continue
                bottom = row.top + row.height
                hovered = index == hover
                if hovered:
                    self._fill(memory, (px(6), row.top, width - px(6), bottom), PALETTE["hover"])
                    bar = PALETTE["danger"] if row.item.danger else PALETTE["accent"]
                    self._fill(memory, (px(6), row.top + px(8), px(6) + px(3), bottom - px(8)), bar)
                if hovered:
                    color = PALETTE["danger_text"] if row.item.danger else PALETTE["text_hover"]
                else:
                    color = PALETTE["text"]
                self._text(memory, row.item.label, (px(22), row.top, width - px(14), bottom), "item", color)

            gdi.BitBlt(hdc, 0, 0, width, height, memory, 0, 0, _SRCCOPY)
        finally:
            gdi.SelectObject(memory, previous)
            gdi.DeleteObject(bitmap)
            gdi.DeleteDC(memory)

    def close(self) -> None:
        """Вернуть шрифты и кисти системе."""
        for handle in (*self._fonts.values(), *self._brushes.values()):
            self._gdi.DeleteObject(handle)
        self._fonts.clear()
        self._brushes.clear()


class StyledMenu:
    """Своё всплывающее меню. Живёт на потоке значка и зовётся только оттуда.

    :param icons: файлы значков по состояниям — реактор в шапке меню.
    """

    def __init__(self, icons: Mapping[str, Path]) -> None:
        self._icon_files = dict(icons)
        self._user32: Any = None
        self._gdi32: Any = None
        self._instance: Any = None
        self._class = f"JarvisTrayMenu{id(self)}"
        self._proc = _WNDPROC(self._window_proc)  # ссылку держим сами, иначе соберёт GC
        self._hwnd: int | None = None
        self._painter: Painter | None = None
        self._layout: PopupLayout | None = None
        self._state = READY
        self._hover: int | None = None
        self._icon: int | None = None
        self._icons: dict[tuple[Path, int], int] = {}
        self._tracking = False
        self._was_active = False
        self._on_choose: Callable[[str], None] | None = None

    def show(
        self,
        cursor: tuple[int, int],
        monitor: int,
        monitor_rect: tuple[int, int, int, int],
        work_rect: tuple[int, int, int, int],
        state: str,
        menu: tuple[MenuItem | None, ...],
        on_choose: Callable[[str], None],
    ) -> bool:
        """Открыть меню у значка. ``False`` — не вышло, пусть покажут системное."""
        if sys.platform != "win32":
            return False
        try:
            self._prepare()
            self.close()
            user32 = self._user32
            scale = self._dpi(monitor) / 96
            layout = popup_layout(menu, scale)
            x, y = popup_position(cursor, monitor_rect, work_rect, (layout.width, layout.height), round(POPUP_GAP * scale))
            self._painter = Painter(user32, self._gdi32, scale)
            self._layout, self._state, self._hover = layout, state, None
            self._icon = self._load_icon(state, self._painter.px(24))
            self._on_choose = on_choose
            self._tracking = self._was_active = False
            hwnd = user32.CreateWindowExW(
                _WS_EX_TOPMOST | _WS_EX_TOOLWINDOW, self._class, "Jarvis", _WS_POPUP,
                x, y, layout.width, layout.height, None, None, self._instance, None,
            )
            if not hwnd:
                raise OSError(f"окно меню не создано ({ctypes.get_last_error()})")
            self._hwnd = hwnd
            _round_corners(hwnd)
            user32.ShowWindow(hwnd, _SW_SHOW)
            # Право на передний план даёт щелчок по значку; без него меню не
            # узнает, что щёлкнули мимо, и не закроется.
            user32.SetForegroundWindow(hwnd)
            user32.SetTimer(hwnd, _TIMER_ID, _WATCH_MS, None)
            return True
        except Exception:  # noqa: BLE001 — без меню оставаться нельзя, есть системное
            logger.exception("Своё меню трея не открылось, показываю системное")
            self.close()
            return False

    def close(self) -> None:
        """Закрыть меню, если открыто."""
        hwnd, self._hwnd = self._hwnd, None
        if hwnd and self._user32 is not None:
            self._user32.KillTimer(hwnd, _TIMER_ID)
            self._user32.DestroyWindow(hwnd)
        if self._painter is not None:
            self._painter.close()
            self._painter = None

    def dispose(self) -> None:
        """Закрыть меню и отдать системе класс окна и значки."""
        self.close()
        if self._user32 is None:
            return
        for icon in self._icons.values():
            self._user32.DestroyIcon(icon)
        self._icons.clear()
        self._user32.UnregisterClassW(self._class, self._instance)

    # --- внутреннее ----------------------------------------------------------

    def _prepare(self) -> None:
        if self._user32 is not None:
            return
        user32, gdi32 = load_libraries()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetModuleHandleW.restype = wintypes.HANDLE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        instance = kernel32.GetModuleHandleW(None)
        window_class = _WNDCLASSW()
        window_class.style = _CS_DROPSHADOW
        window_class.lpfnWndProc = self._proc
        window_class.hInstance = instance
        window_class.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(_IDC_ARROW))
        window_class.lpszClassName = self._class
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            raise OSError(f"класс окна меню не зарегистрирован ({ctypes.get_last_error()})")
        self._user32, self._gdi32, self._instance = user32, gdi32, instance

    @staticmethod
    def _dpi(monitor: int) -> int:
        """Масштаб монитора; нет shcore (старая Windows) — 100%."""
        try:
            shcore = ctypes.WinDLL("shcore")
            shcore.GetDpiForMonitor.argtypes = [
                wintypes.HANDLE, ctypes.c_int, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT),
            ]
            dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
            if shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0 and dpi_x.value:
                return int(dpi_x.value)
        except (OSError, AttributeError):
            pass
        return 96

    def _load_icon(self, state: str, size: int) -> int | None:
        path = self._icon_files.get(state) or self._icon_files.get(READY)
        if path is None:
            return None
        key = (path, size)
        if key not in self._icons:
            icon = self._user32.LoadImageW(None, str(path), _IMAGE_ICON, size, size, _LR_LOADFROMFILE)
            if not icon:
                return None
            self._icons[key] = icon
        return self._icons[key]

    def _dismiss(self) -> None:
        """Спрятать сразу, уничтожить сообщением: изнутри обработки окна так надёжнее."""
        if self._hwnd:
            self._user32.ShowWindow(self._hwnd, _SW_HIDE)
            self._user32.PostMessageW(self._hwnd, _WM_CLOSE, 0, 0)

    def _choose(self, index: int | None) -> None:
        if index is None or self._layout is None:
            return
        item = self._layout.rows[index].item
        callback = self._on_choose
        self._dismiss()
        if item is not None and callback is not None:
            callback(item.action)

    def _set_hover(self, index: int | None) -> None:
        if index != self._hover and self._hwnd:
            self._hover = index
            self._user32.InvalidateRect(self._hwnd, None, False)

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        """Оконная процедура. Падать ей нельзя: исключение уйдёт в ОС."""
        user32 = self._user32
        try:
            if hwnd == self._hwnd and self._layout is not None:
                layout = self._layout
                if message == _WM_PAINT:
                    paint = _PAINTSTRUCT()
                    hdc = user32.BeginPaint(hwnd, ctypes.byref(paint))
                    try:
                        if self._painter is not None:
                            self._painter.paint(hdc, layout, self._state, self._hover, self._icon)
                    finally:
                        user32.EndPaint(hwnd, ctypes.byref(paint))
                    return 0
                if message == _WM_ERASEBKGND:
                    return 1
                if message == _WM_MOUSEMOVE:
                    x = ctypes.c_short(lparam & 0xFFFF).value
                    y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                    self._set_hover(row_at(layout, y) if 0 <= x < layout.width else None)
                    if not self._tracking:
                        track = _TRACKMOUSEEVENT(ctypes.sizeof(_TRACKMOUSEEVENT), _TME_LEAVE, hwnd, 0)
                        self._tracking = bool(user32.TrackMouseEvent(ctypes.byref(track)))
                    return 0
                if message == _WM_MOUSELEAVE:
                    self._tracking = False
                    self._set_hover(None)
                    return 0
                if message in (_WM_LBUTTONUP, _WM_RBUTTONUP):
                    y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                    self._choose(row_at(layout, y))
                    return 0
                if message == _WM_KEYDOWN:
                    if wparam in (_VK_UP, _VK_DOWN):
                        self._set_hover(step_row(layout, self._hover, 1 if wparam == _VK_DOWN else -1))
                    elif wparam == _VK_RETURN:
                        self._choose(self._hover)
                    elif wparam == _VK_ESCAPE:
                        self._dismiss()
                    return 0
                if message == _WM_ACTIVATE:
                    if wparam & 0xFFFF == 0:  # WA_INACTIVE: щёлкнули мимо меню
                        self._dismiss()
                    return 0
                if message == _WM_TIMER:
                    # Запасной сторож: деактивация приходит не всегда, если
                    # фокус ушёл к панели задач.
                    if user32.GetForegroundWindow() == hwnd:
                        self._was_active = True
                    elif self._was_active:
                        self._dismiss()
                    return 0
                if message == _WM_CLOSE:
                    self.close()
                    return 0
            if message == _WM_DESTROY:
                return 0
        except Exception:  # noqa: BLE001 — процедура окна не имеет права падать
            logger.exception("Меню трея: сбой в обработке сообщения %#x", message)
        return int(user32.DefWindowProcW(hwnd, message, wparam, lparam))


def _round_corners(hwnd: int) -> None:
    """Скруглить углы и покрасить рамку (Windows 11); на старой Windows — ничего."""
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
        dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        corner = ctypes.c_int(_DWMWCP_ROUND)
        dwmapi.DwmSetWindowAttribute(hwnd, _DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(corner), ctypes.sizeof(corner))
        border = wintypes.DWORD(colorref(PALETTE["border"]))
        dwmapi.DwmSetWindowAttribute(hwnd, _DWMWA_BORDER_COLOR, ctypes.byref(border), ctypes.sizeof(border))
    except (OSError, AttributeError):
        pass
