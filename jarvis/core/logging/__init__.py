"""Логирование."""

from .colors import ColorFormatter, supports_color
from .setup import setup_logging

__all__ = ["ColorFormatter", "setup_logging", "supports_color"]
