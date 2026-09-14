"""Значок в трее, одиночный запуск и окно с сообщением — через WinAPI.

Зависимостей ноль: всё через ctypes, тем же приёмом, каким скилл `keys` ставит
хук, а `screen` снимает экран. Готовый `pystray` тянул бы Pillow-обвязку и
свой цикл событий ради трёх вызовов ОС.

Значок живёт на **своём потоке** со своим циклом сообщений: окно обязано
получать сообщения там же, где создано, а петля asyncio занята ассистентом.
Всё, что приходит снаружи (смена состояния, выключение), отправляется окну
сообщением, а не вызовом, — трогать окно из чужого потока нельзя.

Вне Windows всё здесь молча ничего не делает: код ядра общий с сервером, а
трея на сервере нет.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from collections.abc import Callable, Mapping
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .menu import DEFAULT_ACTION, MENU, POPUP_GAP, READY, STARTING, MenuItem, menu_commands
from .popup import StyledMenu

logger = logging.getLogger(__name__)

_WM_NULL = 0x0000
_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_WM_CONTEXTMENU = 0x007B
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONUP = 0x0205
_WM_APP = 0x8000
#: Состояние сменилось: перерисовать значок и подпись.
_WM_STATE = _WM_APP + 1
#: Событие мыши над значком — так его называет `uCallbackMessage`.
_WM_TRAY = _WM_APP + 2

_NIM_ADD, _NIM_MODIFY, _NIM_DELETE = 0, 1, 2
_NIF_MESSAGE, _NIF_ICON, _NIF_TIP = 0x1, 0x2, 0x4

_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x10
_SM_CXSMICON, _SM_CYSMICON = 49, 50
_IDI_APPLICATION = 32512

_MF_STRING, _MF_GRAYED, _MF_SEPARATOR = 0x0, 0x1, 0x800
_TPM_RIGHTBUTTON, _TPM_NONOTIFY, _TPM_RETURNCMD = 0x2, 0x80, 0x100
TPM_LEFTALIGN, TPM_RIGHTALIGN, TPM_TOPALIGN, TPM_BOTTOMALIGN = 0x0, 0x8, 0x0, 0x20
TPM_HORIZONTAL, TPM_VERTICAL = 0x0, 0x40
_MONITOR_DEFAULTTONEAREST = 2

Rect = tuple[int, int, int, int]


def menu_placement(
    cursor: tuple[int, int], monitor: Rect, work: Rect, gap: int = 0,
) -> tuple[int, int, int, Rect | None]:
    """Где поставить меню значка, чтобы панель задач его не закрывала.

    Меню у курсора не годится: курсор стоит **на** панели задач, и часть меню
    оказывалась под ней (живой запуск 14.09.2026, в том числе после
    TPM_BOTTOMALIGN). Поэтому меню прижимается к краю рабочей области с той
    стороны, где панель, а сама полоса панели запрещается системе целиком.

    :param cursor: курсор, x и y.
    :param monitor: прямоугольник монитора: слева, сверху, справа, снизу.
    :param work: рабочая область того же монитора, без панели задач.
    :return: x, y, флаги выравнивания и запрещённый прямоугольник или None.
    """
    x, y = cursor
    left, top, right, bottom = monitor
    # Зазор: меню не липнет к панели задач, а висит над ней.
    work_left, work_top = work[0] + gap, work[1] + gap
    work_right, work_bottom = work[2] - gap, work[3] - gap
    x = min(max(x, work_left), work_right)
    if work_bottom < bottom and y >= work_bottom:
        return x, work_bottom, TPM_BOTTOMALIGN | TPM_LEFTALIGN | TPM_VERTICAL, (left, work_bottom, right, bottom)
    if work_top > top and y < work_top:
        return x, work_top, TPM_TOPALIGN | TPM_LEFTALIGN | TPM_VERTICAL, (left, top, right, work_top)
    y = min(max(y, work_top), work_bottom)
    if work_left > left and cursor[0] < work_left:
        return work_left, y, TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_HORIZONTAL, (left, top, work_left, bottom)
    if work_right < right and cursor[0] >= work_right:
        return work_right, y, TPM_RIGHTALIGN | TPM_BOTTOMALIGN | TPM_HORIZONTAL, (work_right, top, right, bottom)
    # Курсор в рабочей области: меню из всплывающего списка скрытых значков.
    return x, y, TPM_BOTTOMALIGN | TPM_LEFTALIGN | TPM_VERTICAL, None


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class _TPMPARAMS(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("rcExclude", wintypes.RECT)]
#: Поток осведомлён о масштабе каждого монитора: курсор и меню в одних пикселях.
_PER_MONITOR_AWARE_V2 = -4
#: `SetPreferredAppMode(AllowDark)` в uxtheme: меню следует тёмной теме Windows.
_APP_MODE_ALLOW_DARK = 1


def _allow_dark_menus() -> None:
    """Разрешить тёмные меню процесса, если в Windows включена тёмная тема.

    Официального API у Windows для этого нет, есть неименованные функции
    uxtheme по номерам (135 — выбрать режим, 136 — перечитать темы меню); ими
    пользуются Проводник и сторонние программы трея. Нет их (старая Windows) —
    меню просто остаётся светлым.
    """
    if sys.platform != "win32":
        return
    try:
        # Any: обращение по номеру у WinDLL есть, но в стабах описано только по имени.
        uxtheme: Any = ctypes.WinDLL("uxtheme")
        set_mode = uxtheme[135]
        set_mode.argtypes = [ctypes.c_int]
        set_mode(_APP_MODE_ALLOW_DARK)
        uxtheme[136]()
    except (OSError, AttributeError):
        pass

_MB_ICONERROR, _MB_ICONINFORMATION, _MB_SETFOREGROUND = 0x10, 0x40, 0x10000

_WAIT_OBJECT_0, _WAIT_ABANDONED = 0x0, 0x80

#: Результат оконной процедуры — размером с указатель, а не 32-битный `int`.
_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_void_p, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = (
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    )


class _NOTIFYICONDATAW(ctypes.Structure):
    """Полная структура, как в Vista и новее: размер сверяется по `cbSize`."""

    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("hWnd", ctypes.c_void_p),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_ubyte * 16),
        ("hBalloonIcon", ctypes.c_void_p),
    )


def _configure(user32: Any, shell32: Any, kernel32: Any) -> None:
    """Объявить типы функций WinAPI.

    Без этого ctypes считает, что всё возвращает 32-битный ``int``, и на
    64-битном Python обрезает дескрипторы окна и значка — ошибка тихая, окно
    просто не находится. Урок из скилла `keys`.
    """
    handle = ctypes.c_void_p
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, handle]
    user32.CreateWindowExW.restype = handle
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        handle, handle, handle, handle,
    ]
    user32.DefWindowProcW.restype = _LRESULT
    user32.DefWindowProcW.argtypes = [handle, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DestroyWindow.argtypes = [handle]
    user32.PostMessageW.argtypes = [handle, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), handle, wintypes.UINT, wintypes.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = _LRESULT
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.RegisterWindowMessageW.restype = wintypes.UINT
    user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    user32.LoadImageW.restype = handle
    user32.LoadImageW.argtypes = [handle, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.LoadIconW.restype = handle
    user32.LoadIconW.argtypes = [handle, handle]
    user32.DestroyIcon.argtypes = [handle]
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.CreatePopupMenu.restype = handle
    user32.AppendMenuW.argtypes = [handle, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    user32.TrackPopupMenu.argtypes = [handle, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, handle, handle]
    user32.TrackPopupMenuEx.argtypes = [handle, wintypes.UINT, ctypes.c_int, ctypes.c_int, handle, ctypes.c_void_p]
    user32.MonitorFromPoint.restype = handle
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.GetMonitorInfoW.argtypes = [handle, ctypes.c_void_p]
    user32.DestroyMenu.argtypes = [handle]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.SetForegroundWindow.argtypes = [handle]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]
    kernel32.GetModuleHandleW.restype = handle
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


class TrayIcon:
    """Значок в области уведомлений с меню по правому щелчку.

    :param on_action: что делать с выбранным пунктом; зовётся **из потока
        значка**, поэтому переправлять работу в петлю asyncio — забота
        вызывающего.
    :param icons: файл значка для каждого состояния; нет файла — стандартный
        значок приложения, но не отказ.
    """

    def __init__(
        self,
        on_action: Callable[[str], None],
        *,
        icons: Mapping[str, Path],
        menu: tuple[MenuItem | None, ...] = MENU,
        default_action: str = DEFAULT_ACTION,
    ) -> None:
        self._on_action = on_action
        self._icon_files = dict(icons)
        self._menu = menu
        self._default_action = default_action
        self._state = STARTING
        self._tip = ""
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._hwnd: int | None = None
        self._user32: Any = None
        self._shell32: Any = None
        self._proc: Any = None  # ссылку держим сами, иначе соберёт GC
        self._icons: dict[str, int] = {}
        self._owned: list[int] = []
        self._taskbar_created = 0
        self._styled: StyledMenu | None = None

    def start(self) -> None:
        """Показать значок. Ждёт, пока окно создано, но не дольше пяти секунд."""
        if sys.platform != "win32" or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def set_state(self, state: str, tip: str) -> None:
        """Сменить вид значка и подпись. Можно звать из любого потока."""
        self._state, self._tip = state, tip
        if self._hwnd and self._user32 is not None:
            self._user32.PostMessageW(self._hwnd, _WM_STATE, 0, 0)

    def stop(self) -> None:
        """Убрать значок и остановить поток."""
        if self._hwnd and self._user32 is not None:
            self._user32.PostMessageW(self._hwnd, _WM_CLOSE, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None

    # --- поток значка --------------------------------------------------------

    def _run(self) -> None:
        """Тело потока: окно, значок и цикл сообщений до закрытия."""
        if sys.platform != "win32":
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure(user32, shell32, kernel32)
        self._user32, self._shell32 = user32, shell32
        # Весь поток значка — в пикселях конкретного монитора. Иначе на экране
        # 125% курсор приходил в «логических» координатах, меню ставилось ниже,
        # чем нужно, и уходило под панель задач (живой запуск 14.09.2026).
        setter = getattr(user32, "SetThreadDpiAwarenessContext", None)
        if setter is not None:
            setter.restype = ctypes.c_void_p
            setter.argtypes = [ctypes.c_void_p]
            setter(ctypes.c_void_p(_PER_MONITOR_AWARE_V2))
        _allow_dark_menus()

        instance = kernel32.GetModuleHandleW(None)
        # Имя класса своё у каждого значка: класс с тем же именем мог остаться
        # от прошлого значка в этом процессе — со ссылкой на мёртвую процедуру.
        class_name = f"JarvisTray{id(self)}"
        self._proc = _WNDPROC(self._window_proc)
        window_class = _WNDCLASSW()
        window_class.lpfnWndProc = self._proc
        window_class.hInstance = instance
        window_class.lpszClassName = class_name
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            logger.warning("Значок в трее: класс окна не зарегистрирован (%d)", ctypes.get_last_error())
            self._ready.set()
            return
        hwnd = user32.CreateWindowExW(0, class_name, "Jarvis", 0, 0, 0, 0, 0, None, None, instance, None)
        if not hwnd:
            logger.warning("Значок в трее: окно не создано (%d)", ctypes.get_last_error())
            user32.UnregisterClassW(class_name, instance)
            self._ready.set()
            return
        self._hwnd = hwnd
        # Проводник, перезапустившись, рассылает это сообщение: все значки
        # пропадают, и каждая программа добавляет свой заново. Без обработки
        # значок исчез бы до конца сеанса.
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self._load_icons()
        self._notify(_NIM_ADD)
        self._ready.set()

        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

        self._hwnd = None
        if self._styled is not None:
            self._styled.dispose()
            self._styled = None
        for icon in self._owned:
            user32.DestroyIcon(icon)
        self._owned.clear()
        user32.UnregisterClassW(class_name, instance)

    def _load_icons(self) -> None:
        """Загрузить файлы значков под мелкий размер трея."""
        user32 = self._user32
        width = user32.GetSystemMetrics(_SM_CXSMICON)
        height = user32.GetSystemMetrics(_SM_CYSMICON)
        fallback = user32.LoadIconW(None, ctypes.c_void_p(_IDI_APPLICATION))
        loaded: dict[Path, int] = {}
        for state, path in self._icon_files.items():
            if path not in loaded:
                icon = user32.LoadImageW(None, str(path), _IMAGE_ICON, width, height, _LR_LOADFROMFILE)
                if icon:
                    self._owned.append(icon)
                else:
                    logger.warning("Значок %s не загрузился, беру стандартный", path)
                loaded[path] = icon or fallback
            self._icons[state] = loaded[path]
        self._icons.setdefault("", fallback)

    def _notify(self, operation: int) -> None:
        """Добавить, обновить или убрать значок."""
        data = _NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        if operation != _NIM_DELETE:
            data.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
            data.uCallbackMessage = _WM_TRAY
            data.hIcon = self._icons.get(self._state) or self._icons.get(READY) or self._icons.get("")
            data.szTip = self._tip
        self._shell32.Shell_NotifyIconW(operation, ctypes.byref(data))

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        """Оконная процедура. Падать ей нельзя: исключение уйдёт в ОС."""
        try:
            if message == _WM_TRAY:
                event = lparam & 0xFFFF
                if event == _WM_LBUTTONDBLCLK:
                    self._fire(self._default_action)
                elif event in (_WM_RBUTTONUP, _WM_CONTEXTMENU):
                    self._popup()
                return 0
            if message == _WM_STATE:
                self._notify(_NIM_MODIFY)
                return 0
            if self._taskbar_created and message == self._taskbar_created:
                self._notify(_NIM_ADD)
                return 0
            if message == _WM_CLOSE:
                self._notify(_NIM_DELETE)
                self._user32.DestroyWindow(hwnd)
                return 0
            if message == _WM_DESTROY:
                self._user32.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001 — процедура окна не имеет права падать
            logger.exception("Значок в трее: сбой в обработке сообщения %#x", message)
        return int(self._user32.DefWindowProcW(hwnd, message, wparam, lparam))

    def _popup(self) -> None:
        """Показать меню над значком и выполнить выбранное."""
        user32 = self._user32
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        monitor = user32.MonitorFromPoint(point, _MONITOR_DEFAULTTONEAREST)
        rects: tuple[Rect, Rect] | None = None
        if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            rect, work = info.rcMonitor, info.rcWork
            rects = (rect.left, rect.top, rect.right, rect.bottom), (work.left, work.top, work.right, work.bottom)
        # Своё меню в цветах панели; не открылось — системное, без меню нельзя.
        if rects is not None:
            if self._styled is None:
                self._styled = StyledMenu(self._icon_files)
            if self._styled.show((point.x, point.y), monitor, *rects, self._state, self._menu, self._fire):
                return
        self._native_popup(point, rects)

    def _native_popup(self, point: Any, rects: tuple[Rect, Rect] | None) -> None:
        """Системное меню — запасной путь, если своё окно не создалось."""
        user32 = self._user32
        menu = user32.CreatePopupMenu()
        actions: dict[int, str] = {}
        # Первой строкой — состояние, серым: ради него к значку и тянутся.
        user32.AppendMenuW(menu, _MF_STRING | _MF_GRAYED, 0, self._tip)
        user32.AppendMenuW(menu, _MF_SEPARATOR, 0, None)
        for command, item in menu_commands(self._menu):
            if item is None:
                user32.AppendMenuW(menu, _MF_SEPARATOR, 0, None)
            else:
                user32.AppendMenuW(menu, _MF_STRING, command, item.label)
                actions[command] = item.action
        # Без переднего плана меню не закрывается щелчком мимо — известная
        # причуда меню у значков, и лечится она ровно так, вместе с WM_NULL.
        user32.SetForegroundWindow(self._hwnd)
        # Меню встаёт над панелью задач, а не у курсора на ней: см. menu_placement.
        x, y, align, exclude = point.x, point.y, TPM_BOTTOMALIGN | TPM_VERTICAL, None
        if rects is not None:
            getter = getattr(user32, "GetDpiForWindow", None)
            dpi = int(getter(self._hwnd)) if getter is not None else 96
            x, y, align, exclude = menu_placement((point.x, point.y), *rects, gap=round(POPUP_GAP * dpi / 96))
        params = _TPMPARAMS()
        params.cbSize = ctypes.sizeof(_TPMPARAMS)
        if exclude is not None:
            params.rcExclude = wintypes.RECT(*exclude)
        chosen = user32.TrackPopupMenuEx(
            menu, _TPM_RIGHTBUTTON | _TPM_RETURNCMD | _TPM_NONOTIFY | align,
            x, y, self._hwnd, ctypes.byref(params) if exclude is not None else None,
        )
        user32.PostMessageW(self._hwnd, _WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if chosen in actions:
            self._fire(actions[chosen])

    def _fire(self, action: str) -> None:
        """Выполнить действие, не уронив поток значка."""
        try:
            self._on_action(action)
        except Exception:  # noqa: BLE001 — сбой пункта меню не должен гасить значок
            logger.exception("Значок в трее: действие %s не выполнилось", action)


class SingleInstance:
    """Не дать запуститься второму Jarvis: два ассистента делили бы микрофон.

    Держится именованным мьютексом. Процесс умер — ОС освобождает мьютекс
    сама, поэтому упавший Jarvis не запирает следующий запуск.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._handle: int | None = None
        self._kernel32: Any = None

    def acquire(self, wait_s: float = 0.0) -> bool:
        """Занять место. ``False`` — Jarvis уже работает и за `wait_s` не вышел."""
        if sys.platform != "win32":
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.CreateMutexW(None, False, self._name)
        if not handle:
            # Спросить не вышло — запуску не мешаем: лишний Jarvis лучше, чем
            # ни одного.
            logger.warning("Проверка второго запуска недоступна (%d)", ctypes.get_last_error())
            return True
        if kernel32.WaitForSingleObject(handle, int(wait_s * 1000)) in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
            self._handle, self._kernel32 = handle, kernel32
            return True
        kernel32.CloseHandle(handle)
        return False

    def release(self) -> None:
        """Освободить место — перед перезапуском, чтобы новый процесс не ждал."""
        if self._handle and self._kernel32 is not None:
            self._kernel32.ReleaseMutex(self._handle)
            self._kernel32.CloseHandle(self._handle)
        self._handle = None


def message_box(text: str, *, title: str = "Jarvis", error: bool = False) -> None:
    """Показать окно с сообщением. Консоли нет, и иначе ошибку не увидит никто."""
    if sys.platform != "win32":
        print(text, file=sys.stderr)
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.MessageBoxW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    kind = _MB_ICONERROR if error else _MB_ICONINFORMATION
    user32.MessageBoxW(None, text, title, kind | _MB_SETFOREGROUND)
