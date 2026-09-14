"""Значок в трее: Jarvis без окна консоли.

`menu` — что показывается, `win32` — как это рисует Windows, `session` —
как значок связан с приложением.
"""

from .menu import MENU, READY, STARTING, STOPPING, MenuItem, tip
from .session import TraySession, run_in_tray

__all__ = [
    "MENU",
    "READY",
    "STARTING",
    "STOPPING",
    "MenuItem",
    "TraySession",
    "run_in_tray",
    "tip",
]
