"""Работа с текстом, который пришёл из речи."""

from .spoken import CYRILLIC_TO_LATIN, PHONETIC, romanize, skeleton, squash

__all__ = ["CYRILLIC_TO_LATIN", "PHONETIC", "romanize", "skeleton", "squash"]
