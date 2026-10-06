"""Резолвер синонимов и нечётких совпадений — второе звено, тоже без сети.

Ловит два случая: явные синонимы из конфига (``router.aliases``) и опечатки
распознавания, когда Whisper услышал почти правильную фразу.
"""

from __future__ import annotations

import difflib
import logging
import re
from typing import Mapping

from jarvis.core.contracts import Intent, Utterance
from jarvis.core.tools import ToolRegistry

from ..templates import filler
from .phrase import recognized

logger = logging.getLogger(__name__)

#: Насколько похожей должна быть фраза, чтобы считаться той же командой.
_SIMILARITY = 0.82
#: Числа сверяются как есть: «3 часа» и «полчаса» — разные сроки, а не опечатка.
_DIGITS = re.compile(r"\d")


def same_meaning(said: list[str], phrase: list[str]) -> bool:
    """Одна ли это просьба с точностью до опечаток и служебных слов.

    Нечёткое сравнение целой строки отвечает только «насколько похоже», и
    лишнее слово лишь чуть опускает похожесть. Живой случай 06.10.2026, 18:49:
    «не слушай **3 часа**» подобралось к объявленной «не слушай **полчаса**»
    (0.85) — и ассистент замолчал на тридцать минут. Это даже не потеря срока,
    а подмена его другим, и звучит она уверенно.

    Поэтому поверх похожести стоят два условия, и оба про смысл, а не про буквы:

    * **числа обязаны совпасть ровно** — подменять их нечем;
    * **лишние слова обязаны быть служебными** (`FILLER`): опечатку они не
      создают, а «не ставь таймер» против «поставь таймер» — создают, и
      раньше это ставило таймер.

    Чистая функция — её проверяют тесты.
    """
    if sorted(word for word in said if _DIGITS.search(word)) != sorted(
        word for word in phrase if _DIGITS.search(word)
    ):
        return False
    extra = len(said) - len(phrase)
    if extra <= 0:
        return True
    # Слова фразы могут быть сказаны с опечаткой, поэтому лишними считаем те,
    # которых во фразе нет вовсе, — и каждое обязано быть служебным.
    unknown = [word for word in said if word not in phrase]
    return len(unknown) >= extra and all(filler(word) for word in unknown[:extra])


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

        matches = difflib.get_close_matches(text, list(index), n=5, cutoff=self._similarity)
        # Инструмент, отказавшийся от фразы, не совпадение и здесь: команда
        # набора для закрытой программы («сохрани проект» без FL Studio)
        # отсеялась у точных фраз и тут же вернулась похожей (29.09.2026).
        matches = [match for match in matches if recognized(self._registry, index[match], {})]
        if not matches:
            return None

        best = matches[0]
        if not same_meaning(text.split(), best.split()):
            # Похоже по буквам, но просьба другая: лишний срок, число, отрицание.
            logger.debug("Похоже на %r, но смысл другой: %r", best, text)
            return None
        score = difflib.SequenceMatcher(None, text, best).ratio()
        logger.debug("Нечёткое совпадение: %r ~ %r (%.2f)", text, best, score)
        return Intent(
            tool=index[best],
            confidence=score,
            resolver=self.name,
            utterance=utterance.text,
        )
