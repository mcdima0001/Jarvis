"""Работа с текстом, который пришёл из речи."""

from .matching import (
    ENDINGS,
    LEAST,
    LEAST_SKELETON,
    best_match,
    closeness,
    forms,
    rank,
    shared_word,
    similarity,
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
    "rank",
    "romanize",
    "shared_word",
    "similarity",
    "skeleton",
    "sounds_alike",
    "squash",
    "starts",
    "stem",
    "touches",
]
