"""Панель управления: окно с состоянием и настройками Jarvis.

`panel` — сервис и запросы, `http` — сервер, `settings` — правка файлов,
`static/index.html` — сама страница.
"""

from .panel import ControlPanel

__all__ = ["ControlPanel"]
