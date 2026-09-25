r"""Точные руки: нажать в окне программы **по названию**, а не мышью по пикселям.

Стенд `tools/agent_bench` 25.09.2026: план, дойдя до «нажми кнопку», тянулся к
мыши «в текущей точке указателя» — то есть вслепую, потому что координат ему
никто не давал. Зрячая модель в пикселях ошибается, а вот Windows знает о
каждой кнопке точно: её **дерево доступности** (UI Automation) — тот же
источник, по которому экранный диктор читает интерфейс слепым.

Замер на калькуляторе владельца: 37 нажимаемых элементов с именами за
**0.04 с**, «Семь» нажата по имени за **0.02 с** — курсор не шелохнулся.
Нажатие идёт шаблоном элемента (Invoke, Toggle, Select), а не щелчком: так
кнопка срабатывает, даже если окно перекрыто другим.

Ограничения честные: элементы без имени (иконки без подписи) не видны, а
программы на своих движках отрисовки (игры, часть Electron) дерева почти не
отдают. Там остаётся мышь.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Сколько элементов отдавать плану. Больше — дороже в токенах, а нужная кнопка
#: почти всегда среди первых: дерево идёт сверху вниз, как их видит глаз.
LIMIT = 40

#: Окна, в которых руки не работают вовсе: пароли и деньги. Тот же принцип,
#: что у наблюдателя клавиатуры (`keys`): в такие окна ассистент не лезет.
FORBIDDEN_WINDOWS = (
    "парол", "password", "keepass", "bitwarden", "1password", "lastpass",
    "банк", "bank", "сбербанк", "тинькофф", "т-банк", "альфа-банк", "втб",
)

#: Виды элементов, которые имеет смысл нажимать или заполнять.
_KINDS = {
    "Button": "кнопка", "Edit": "поле", "ListItem": "пункт", "Hyperlink": "ссылка",
    "MenuItem": "меню", "TabItem": "вкладка", "CheckBox": "флажок", "ComboBox": "список",
    "TreeItem": "пункт дерева", "RadioButton": "переключатель", "SplitButton": "кнопка",
    "Document": "документ",
}


class HandsError(Exception):
    """Нажать не вышло — и понятно почему. Текст идёт вслух."""


@dataclass(frozen=True, slots=True)
class Element:
    """Элемент окна так, как его увидит план."""

    kind: str
    name: str


def _uia() -> tuple[Any, Any]:
    """Модуль и объект UI Automation для **текущего** потока.

    COM привязан к потоку, а инструменты зовут нас из пула `asyncio.to_thread`,
    где потоки разные. Поэтому инициализация на каждый вызов: повторная для
    того же потока ничего не стоит, а объект автоматизации поднимается за
    сотые доли секунды (замер: 0.03 с).
    """
    import comtypes
    import comtypes.client

    comtypes.CoInitialize()
    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as uia

    automation = comtypes.client.CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)
    return uia, automation


def _kind_names(uia: Any) -> dict[int, str]:
    return {
        getattr(uia, f"UIA_{name}ControlTypeId"): spoken
        for name, spoken in _KINDS.items()
        if hasattr(uia, f"UIA_{name}ControlTypeId")
    }


def forbidden(title: str) -> bool:
    """Окно, куда руки не суются. Чистая функция — её проверяют тесты."""
    lowered = title.lower()
    return any(word in lowered for word in FORBIDDEN_WINDOWS)


def _window(uia: Any, automation: Any, title: str) -> Any:
    """Окно по заголовку (как расслышали) или то, что сейчас впереди."""
    if not title:
        import ctypes

        handle = ctypes.WinDLL("user32").GetForegroundWindow()
        if not handle:
            raise HandsError("впереди нет окна")
        return automation.ElementFromHandle(handle)

    from jarvis.core.text import best_match

    root = automation.GetRootElement()
    children = root.FindAll(uia.TreeScope_Children, automation.CreateTrueCondition())
    named = {}
    for index in range(children.Length):
        element = children.GetElement(index)
        if element.CurrentName:
            named[element.CurrentName] = element
    found = best_match(title, list(named), similarity=0.5)
    if found is None:
        raise HandsError(f"не нашёл окно «{title}»")
    return named[found]


def _collect(uia: Any, automation: Any, window: Any) -> list[tuple[Element, Any]]:
    kinds = _kind_names(uia)
    found = window.FindAll(uia.TreeScope_Descendants, automation.CreateTrueCondition())
    seen: set[tuple[str, str]] = set()
    items: list[tuple[Element, Any]] = []
    for index in range(found.Length):
        element = found.GetElement(index)
        kind = kinds.get(element.CurrentControlType)
        name = (element.CurrentName or "").strip()
        if not kind or not name or element.CurrentIsOffscreen:
            continue
        key = (kind, name)
        if key in seen:
            continue
        seen.add(key)
        items.append((Element(kind=kind, name=name), element))
    return items


def _guard(window: Any) -> str:
    title = window.CurrentName or ""
    if forbidden(title):
        raise HandsError(f"в окно «{title}» руками не лезу: там пароли или деньги")
    return title


def elements(title: str = "") -> tuple[str, list[Element]]:
    """Что можно нажать или заполнить в окне. Возвращает заголовок и элементы."""
    uia, automation = _uia()
    window = _window(uia, automation, title)
    name = _guard(window)
    return name, [element for element, _ in _collect(uia, automation, window)][:LIMIT]


def _pick(items: list[tuple[Element, Any]], wanted: str, *, kinds: tuple[str, ...] = ()) -> tuple[Element, Any]:
    from jarvis.core.text import best_match

    pool = [(element, raw) for element, raw in items if not kinds or element.kind in kinds]
    names = {element.name: (element, raw) for element, raw in pool}
    found = best_match(wanted, list(names), similarity=0.6)
    if found is None:
        near = ", ".join(list(names)[:6])
        raise HandsError(f"не нашёл «{wanted}»" + (f"; есть: {near}" if near else ""))
    return names[found]


def press(wanted: str, title: str = "") -> str:
    """Нажать элемент по названию. Возвращает, что именно нажато.

    Пробуем то, что элемент умеет сам: вызвать, переключить, выбрать,
    раскрыть. Мышью не щёлкаем: для неё нужны координаты и открытое окно, а
    шаблон срабатывает и у перекрытого.
    """
    from jarvis.core.risk import risky

    uia, automation = _uia()
    window = _window(uia, automation, title)
    _guard(window)
    element, raw = _pick(_collect(uia, automation, window), wanted)
    if risky(element.name):
        raise HandsError(f"«{element.name}» — необратимо, нажму только по прямой команде")

    attempts = (
        (uia.UIA_InvokePatternId, uia.IUIAutomationInvokePattern, "Invoke"),
        (uia.UIA_TogglePatternId, uia.IUIAutomationTogglePattern, "Toggle"),
        (uia.UIA_SelectionItemPatternId, uia.IUIAutomationSelectionItemPattern, "Select"),
        (uia.UIA_ExpandCollapsePatternId, uia.IUIAutomationExpandCollapsePattern, "Expand"),
        (uia.UIA_LegacyIAccessiblePatternId, uia.IUIAutomationLegacyIAccessiblePattern, "DoDefaultAction"),
    )
    for pattern_id, interface, method in attempts:
        pattern = raw.GetCurrentPattern(pattern_id)
        if not pattern:
            continue
        try:
            getattr(pattern.QueryInterface(interface), method)()
        except Exception as exc:  # noqa: BLE001 — пробуем следующий способ
            logger.debug("Нажатие %s через %s не прошло: %s", element.name, method, exc)
            continue
        logger.info("Нажал «%s» (%s) через %s", element.name, element.kind, method)
        return f"{element.kind} «{element.name}»"
    raise HandsError(f"«{element.name}» нажать нечем: элемент не принимает команд")


def write(text: str, field: str = "", title: str = "") -> str:
    """Вписать текст в поле по названию поля (или в первое поле окна).

    Значение ставится шаблоном элемента, а не набором на клавиатуре: текст
    не уйдёт в чужое окно, если фокус внезапно сменится. Enter не нажимается —
    отправить что-то вписанное должен владелец.
    """
    uia, automation = _uia()
    window = _window(uia, automation, title)
    _guard(window)
    items = _collect(uia, automation, window)
    fields = tuple(kind for kind in ("поле", "список", "документ"))
    if field:
        element, raw = _pick(items, field, kinds=fields)
    else:
        found = [(element, raw) for element, raw in items if element.kind in fields]
        if not found:
            raise HandsError("в окне нет поля для текста")
        element, raw = found[0]
    pattern = raw.GetCurrentPattern(uia.UIA_ValuePatternId)
    if not pattern:
        raise HandsError(f"в «{element.name}» текст не вписать")
    pattern.QueryInterface(uia.IUIAutomationValuePattern).SetValue(text)
    logger.info("Вписал в «%s» %d знаков", element.name, len(text))
    return element.name


def available() -> bool:
    """Есть ли дерево доступности на этой машине."""
    if sys.platform != "win32":
        return False
    try:
        _uia()
    except Exception:  # noqa: BLE001 — нет comtypes или COM — рук нет
        return False
    return True
