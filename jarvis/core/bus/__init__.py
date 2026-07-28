"""Шина событий."""

from .local import LocalEventBus
from .protocol import EventBus, EventHandler, Subscription

__all__ = ["EventBus", "EventHandler", "LocalEventBus", "Subscription"]
