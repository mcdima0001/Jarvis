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


#: Меню по правому щелчку; ``None`` — разделитель. Выход стоит последним, а
#: перезапуск отделён от безобидных пунктов: промахнуться по нему дороже всего.
MENU: tuple[MenuItem | None, ...] = (
    MenuItem("Открыть панель", "panel"),
    MenuItem("Показать лог", "log"),
    MenuItem("Открыть папку Jarvis", "folder"),
    None,
    MenuItem("Перезапустить", "restart"),
    MenuItem("Выйти", "quit"),
)

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
