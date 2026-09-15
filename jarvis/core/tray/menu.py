"""Что показывает значок в трее: состояния, подпись и меню.

Отдельно от WinAPI намеренно: здесь только данные и чистые функции, и их можно
проверить на любой машине. Сам значок (`win32.py`) — тонкая обёртка, которая
эти данные рисует.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Модели и скиллы грузятся: на Vosk это полторы минуты, и всё это время
#: ассистент не слышит. Без отдельного вида значок врал бы, что готов.
STARTING = "starting"
#: Всё поднято, ассистент слушает.
READY = "ready"
#: Выключается: прощание и остановка сервисов занимают секунды.
STOPPING = "stopping"

#: Подпись под значком для каждого состояния.
STATE_WORDS = {
    STARTING: "загружается…",
    READY: "слушает",
    STOPPING: "выключается…",
}

#: Подпись значка ограничена ОС: 128 знаков вместе с завершающим нулём.
TIP_LIMIT = 127

#: С какого номера нумеровать пункты меню. Ноль занят: его возвращает ОС,
#: когда меню закрыли, ничего не выбрав.
FIRST_COMMAND = 1000


@dataclass(frozen=True)
class MenuItem:
    """Пункт меню: что написано и какое действие выполняет."""

    label: str
    action: str
    #: Подсветить красным: действие, которое гасит ассистента.
    danger: bool = False
    #: Переключатель: рядом с подписью галочка, если включён.
    toggle: bool = False


#: Действие переключателя «запускать с Windows».
AUTOSTART = "autostart"

#: Меню по правому щелчку; ``None`` — разделитель. Выход стоит последним, а
#: перезапуск отделён от безобидных пунктов: промахнуться по нему дороже всего.
MENU: tuple[MenuItem | None, ...] = (
    MenuItem("Открыть панель", "panel"),
    MenuItem("Показать лог", "log"),
    MenuItem("Открыть папку Jarvis", "folder"),
    None,
    MenuItem("Запускать с Windows", AUTOSTART, toggle=True),
    None,
    MenuItem("Перезапустить", "restart"),
    MenuItem("Выйти", "quit", danger=True),
)

# --- своё меню (popup.py) ----------------------------------------------------

#: Цвета своего меню — те же, что у панели (`gui/static/index.html`).
PALETTE = {
    "background": "#07131c",
    "hover": "#0c2a39",
    "line": "#15394a",
    "border": "#1d5569",
    "accent": "#4fd8ff",
    "text": "#d6f4ff",
    "text_hover": "#8fe9ff",
    "muted": "#6f97a8",
    "danger": "#ff5a5a",
    "danger_text": "#ff8a8a",
}
#: Точка состояния в шапке меню.
STATE_COLORS = {STARTING: "#ffa928", READY: "#6ee7a0", STOPPING: "#ff5a5a"}
#: Зазор между меню и панелью задач, в точках при масштабе 100%.
POPUP_GAP = 8


@dataclass(frozen=True)
class PopupRow:
    """Строка своего меню: пункт или разделитель (``item is None``)."""

    top: int
    height: int
    item: MenuItem | None


@dataclass(frozen=True)
class PopupLayout:
    """Размеры своего меню в пикселях монитора."""

    width: int
    height: int
    header: int
    rows: tuple[PopupRow, ...]


def popup_layout(menu: tuple[MenuItem | None, ...] = MENU, scale: float = 1.0) -> PopupLayout:
    """Разложить меню: шапка, пункты по 34 точки, разделители по 9."""
    def px(value: float) -> int:
        return round(value * scale)

    header, padding = px(56), px(6)
    top = header + padding
    rows: list[PopupRow] = []
    for item in menu:
        height = px(9) if item is None else px(34)
        rows.append(PopupRow(top, height, item))
        top += height
    return PopupLayout(width=px(248), height=top + padding, header=header, rows=tuple(rows))


def row_at(layout: PopupLayout, y: int) -> int | None:
    """Какой пункт под мышью; разделитель и шапка — ни один."""
    for index, row in enumerate(layout.rows):
        if row.item is not None and row.top <= y < row.top + row.height:
            return index
    return None


def step_row(layout: PopupLayout, current: int | None, delta: int) -> int | None:
    """Следующий пункт при нажатии стрелки: мимо разделителей, по кругу."""
    items = [index for index, row in enumerate(layout.rows) if row.item is not None]
    if not items:
        return None
    if current not in items:
        return items[0] if delta > 0 else items[-1]
    return items[(items.index(current) + delta) % len(items)]


def popup_position(
    cursor: tuple[int, int],
    monitor: tuple[int, int, int, int],
    work: tuple[int, int, int, int],
    size: tuple[int, int],
    gap: int,
) -> tuple[int, int]:
    """Левый верхний угол меню: над панелью задач с зазором, по центру над значком.

    Панель задач бывает у любого края; с какого — видно по тому, чем рабочая
    область меньше монитора. Курсор в рабочей области — меню открыто из списка
    скрытых значков, тогда оно встаёт над курсором.
    """
    x, y = cursor
    left, top, right, bottom = monitor
    work_left, work_top, work_right, work_bottom = work
    width, height = size

    def fit_x(value: int) -> int:
        return max(work_left + gap, min(value, work_right - gap - width))

    def fit_y(value: int) -> int:
        return max(work_top + gap, min(value, work_bottom - gap - height))

    if work_bottom < bottom and y >= work_bottom:
        return fit_x(x - width // 2), work_bottom - gap - height
    if work_top > top and y < work_top:
        return fit_x(x - width // 2), work_top + gap
    if work_left > left and x < work_left:
        return work_left + gap, fit_y(y - height // 2)
    if work_right < right and x >= work_right:
        return work_right - gap - width, fit_y(y - height // 2)
    return fit_x(x - width // 2), fit_y(y - gap - height)

#: Что делает двойной щелчок по значку: панель — там и статус, и лог.
DEFAULT_ACTION = "panel"


def tip(name: str, state: str) -> str:
    """Подпись под значком: «Jarvis — слушает»."""
    return f"{name} — {STATE_WORDS.get(state, state)}"[:TIP_LIMIT]


def menu_commands(menu: tuple[MenuItem | None, ...] = MENU) -> list[tuple[int, MenuItem | None]]:
    """Пункты меню с номерами команд; разделитель номера не получает (0)."""
    return [
        (0 if item is None else FIRST_COMMAND + index, item)
        for index, item in enumerate(menu)
    ]
