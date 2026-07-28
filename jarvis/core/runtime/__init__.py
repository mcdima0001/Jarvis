"""Исполнительная среда: пул для блокирующих задач."""

from .worker import BlockingWorker

__all__ = ["BlockingWorker"]
