"""Работа с текстом, который пришёл из речи."""

from .matching import (
    ENDINGS,
    LEAST,
    LEAST_SKELETON,
    best_match,
    closeness,
    forms,
    shared_word,
    sounds_alike,
    starts,
    stem,
    touches,
)
from .spoken import CYRILLIC_TO_LATIN, PHONETIC, romanize, skeleton, squash

__all__ = [
    "CYRILLIC_TO_LATIN",
    "ENDINGS",
    "LEAST",
    "LEAST_SKELETON",
    "PHONETIC",
    "best_match",
    "closeness",
    "forms",
    "romanize",
    "shared_word",
    "skeleton",
    "sounds_alike",
    "squash",
    "starts",
    "stem",
    "touches",
]
