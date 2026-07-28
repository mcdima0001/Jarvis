"""Резолвер синонимов и нечётких совпадений — второе звено, тоже без сети.

Ловит два случая: явные синонимы из конфига (``router.aliases``) и опечатки
распознавания, когда Whisper услышал почти правильную фразу.
"""

from __future__ import annotations

import difflib
import logging
from typing import Mapping

from jarvis.core.contracts import Intent, Utterance
from jarvis.core.tools import ToolRegistry

logger = logging.getLogger(__name__)

#: Насколько похожей должна быть фраза, чтобы считаться той же командой.
_SIMILARITY = 0.82


class AliasResolver:
    """Синонимы из конфига плюс нечёткое сравнение с фразами скиллов."""

    def __init__(
        self,
        registry: ToolRegistry,
        aliases: Mapping[str, str],
        *,
        similarity: float = _SIMILARITY,
    ) -> None:
        self._registry = registry
        self._aliases = {" ".join(k.lower().split()): v for k, v in aliases.items()}
        self._similarity = similarity

    @property
    def name(self) -> str:
        """Имя резолвера."""
        return "alias"

    async def resolve(self, utterance: Utterance) -> Intent | None:
        """Сопоставить реплику с синонимом или похожей фразой."""
        text = utterance.normalized
        if not text:
            return None

        target = self._aliases.get(text)
        if target is not None:
            if not self._registry.has(target):
                logger.warning("Синоним %r ведёт на неизвестный инструмент %r", text, target)
            else:
                return Intent(
                    tool=target,
                    confidence=0.9,
                    resolver=self.name,
                    utterance=utterance.text,
                )

        index = self._registry.phrase_index()
        if not index:
            return None

        matches = difflib.get_close_matches(text, list(index), n=1, cutoff=self._similarity)
        if not matches:
            return None

        best = matches[0]
        score = difflib.SequenceMatcher(None, text, best).ratio()
        logger.debug("Нечёткое совпадение: %r ~ %r (%.2f)", text, best, score)
        return Intent(
            tool=index[best],
            confidence=score,
            resolver=self.name,
            utterance=utterance.text,
        )
