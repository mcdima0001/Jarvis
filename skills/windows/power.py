"""Питание, память и клавиши: то, чего скиллу `windows` не хватало для игры.

Три вещи, которые понадобились игровому режиму и не нашлись нигде: переключить
схему электропитания, узнать, кто держит память, и нажать сочетание клавиш за
человека. Все три — про машину, поэтому живут рядом с остальным про машину, а не
отдельным скиллом: игровой режим — это **набор шагов в протоколе**, а не новая
подсистема.

Замер 23.09.2026, ради которого всё затевалось (машина 16.9 ГБ, занято 82%):

| программа | память | процессов |
|---|---|---|
| Claude | **2.03 ГБ** | 16 |
| браузер | **1.89 ГБ** | 30 |
| сам Jarvis | 1.00 ГБ | 5 |
| Telegram (AyuGram) | 0.27 ГБ | 1 |

Отсюда и правило «сказать, а не закрыть»: весь выигрыш сидит в двух программах,
полных несохранённой работы, а телега — это полтора процента машины.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence

#: Схемы электропитания Windows. GUID у них одинаковые на всех машинах.
POWER_PLANS: Mapping[str, str] = {
    "performance": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
    "balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
    "saver": "a1841308-3541-4fab-bc81-f71556f20b4a",
}

#: Как схему называют вслух.
PLAN_WORDS: Mapping[str, str] = {
    "производительность": "performance", "производительности": "performance",
    "максимальная": "performance", "performance": "performance",
    "сбалансированная": "balanced", "баланс": "balanced", "balanced": "balanced",
    "экономия": "saver", "энергосбережение": "saver", "saver": "saver",
}


def power_plan(guid: str = "") -> str:
    """Текущая схема питания; с `guid` — переключить и вернуть прежнюю.

    :return: GUID схемы, которая была активна до вызова. Пусто — не вышло.
    """
    if sys.platform != "win32":
        return ""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(
            ["powercfg", "/getactivescheme"],
            capture_output=True, timeout=10, check=False, creationflags=flags,
        )
        text = done.stdout.decode("cp866", "replace")
        was = ""
        for piece in text.split():
            if piece.count("-") == 4 and len(piece) == 36:
                was = piece
                break
        if guid and was != guid:
            subprocess.run(
                ["powercfg", "/setactive", guid],
                capture_output=True, timeout=10, check=False, creationflags=flags,
            )
        return was
    except (OSError, subprocess.SubprocessError):
        return ""


def press(combination: str) -> bool:
    """Нажать сочетание клавиш — например, чтобы включить OSD у Afterburner.

    Своей ручки для этого у Afterburner нет: общая память, из которой мы читаем
    графики, работает только на чтение. Зато горячая клавиша есть у него самого
    и у RTSS — её и нажимаем, а какая именно, задаёт владелец в настройках.
    """
    keys = key_codes(combination)
    if sys.platform != "win32" or not keys:
        return False
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x0001, 0x0002
    # Скан-код вместе с виртуальным: часть игр читает клавиатуру по скан-кодам
    # (DirectInput) и нажатие с одним виртуальным кодом не видит вовсе.
    scans = [user32.MapVirtualKeyW(key, 0) for key in keys]
    extended = [KEYEVENTF_EXTENDEDKEY if key in _EXTENDED else 0 for key in keys]
    for key, scan, flag in zip(keys, scans, extended, strict=True):
        user32.keybd_event(key, scan, flag, 0)
    for key, scan, flag in reversed(list(zip(keys, scans, extended, strict=True))):
        user32.keybd_event(key, scan, flag | KEYEVENTF_KEYUP, 0)
    return True


#: Названия клавиш → виртуальные коды Windows. Буквы и цифры — сами собой.
KEY_NAMES = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    **{f"f{number}": 0x6F + number for number in range(1, 25)},
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "pause": 0x13, "capslock": 0x14,
    "plus": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE,
    "num0": 0x60, "num1": 0x61, "num2": 0x62, "num3": 0x63, "num4": 0x64,
    "num5": 0x65, "num6": 0x66, "num7": 0x67, "num8": 0x68, "num9": 0x69,
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "divide": 0x6F,
    "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
    "nexttrack": 0xB0, "prevtrack": 0xB1, "stop": 0xB2, "playpause": 0xB3,
}

#: Клавиши из «серого» блока: без флага расширенной клавиши стрелка читается
#: как цифра на цифровом блоке.
_EXTENDED = frozenset({0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x6F})


def key_codes(combination: str) -> list[int]:
    """«ctrl+shift+m» → виртуальные коды по порядку; пусто — не разобрали.

    Чистая функция: её проверяют тесты на любой машине.
    """
    keys: list[int] = []
    for part in combination.lower().replace(" ", "").split("+"):
        if part in KEY_NAMES:
            keys.append(KEY_NAMES[part])
        elif len(part) == 1 and (part.isascii() and part.isalnum()):
            keys.append(ord(part.upper()))
        else:
            return []
    return keys


def hungry(names: Sequence[str], sizes: Mapping[str, float], least_gb: float) -> list[str]:
    """Кого из названных стоит упоминать вслух: тех, кто держит заметную память.

    Чистая функция, потому что правило спорное и меняться будет: сказать про
    двести мегабайт — значит отвлечь человека ради полутора процентов машины.
    """
    return [name for name in names if sizes.get(name.lower(), 0.0) >= least_gb]


def memory_of(names: Iterable[str] | None = None) -> dict[str, float]:
    """Сколько держит каждая программа, в гигабайтах.

    Считается по всем процессам программы: у браузера их три десятка, и по
    одному судить бессмысленно — ровно поэтому диспетчер задач и складывает.

    :param names: кого считать; ``None`` — всех, кто нашёлся.
    """
    try:
        import psutil
    except ImportError:  # pragma: no cover — на машине владельца он есть
        return {}
    wanted = {name.lower() for name in names} if names is not None else None
    found: dict[str, float] = {}
    for process in psutil.process_iter(["name", "memory_info"]):
        try:
            name = (process.info["name"] or "").lower()
            if not name or (wanted is not None and name not in wanted):
                continue
            found[name] = found.get(name, 0.0) + process.info["memory_info"].rss / 1e9
        except Exception:  # noqa: BLE001 — процесс мог умереть между вызовами
            continue
    return found


def running(names: Iterable[str]) -> set[str]:
    """Какие из названных процессов сейчас запущены.

    По списку имён, а не по полноэкранному окну: полный экран — признак
    обманчивый, ровно так же выглядит фильм, и протокол срабатывал бы посреди
    кино.
    """
    try:
        import psutil
    except ImportError:  # pragma: no cover
        return set()
    wanted = {name.lower() for name in names}
    found: set[str] = set()
    for process in psutil.process_iter(["name"]):
        try:
            name = (process.info["name"] or "").lower()
        except Exception:  # noqa: BLE001 — процесс мог умереть между вызовами
            continue
        if name in wanted:
            found.add(name)
    return found


def spoken_name(process: str, names: Mapping[str, str]) -> str:
    """Как программа называется вслух: «browser.exe» человеку ничего не говорит."""
    low = process.lower()
    return names.get(low, names.get(low.removesuffix(".exe"), low.removesuffix(".exe")))


