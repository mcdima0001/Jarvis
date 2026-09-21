"""Ослышка в одном слове — «табах басов» вместо «добавь басов».

Стоит **после** точных фраз, диктовки и выученного и **перед** моделью: ловит
ровно то, что иначе уехало бы в облако. Причина живая — распознавание коверкает
слово, команда перестаёт узнаваться, и ассистент, у которого эта команда есть,
отвечает «не понял». Владелец 21.09.2026: «он постоянно не понимает, о чём я».

**Правило нарочно узкое: ослышаться можно ровно в одном слове.** Остальные
слова обязаны совпасть — точно, по основе или по звучанию. Нечёткость «в
среднем по фразе» даёт ровно то, чего допускать нельзя: «убери» превращается в
«убери басы», а короткая реплика находит себе длинную команду по случайным
буквам. Ложное срабатывание тут дороже пропуска: пропущенную команду повторяют,
а выполненную не туда — отменяют.

Шаблоны со слотами («поставь пресет {name}») сюда не идут: в слоте живёт
произвольное название, и «похожесть» там означала бы совсем другое.
"""

from __future__ import annotations

import logging

from jarvis.core.contracts import Intent, Utterance
from jarvis.core.text.matching import closeness, sounds_alike, stem
from jarvis.core.tools import ToolRegistry

from ..templates import compile_template as _compile

logger = logging.getLogger(__name__)

#: Насколько похожим должно быть ослышанное слово по буквам. Порог низкий
#: намеренно: настоящая ослышка по буквам похожа **слабо** («табах» против
#: «добавь» — 0.36, «босов» против «басы» — 0.44), а чужая команда, наоборот,
#: сильно («телеграм» против «телефон» — 0.53, «закрой» против «открой» —
#: 0.67). То есть буквами эти два случая не разделяются вовсе, и главный
#: признак тут другой — `_known_word`.
THRESHOLD = 0.33
#: Насколько сопоставимы должны быть длины слов: «бас» и «баланс» иначе
#: считаются похожими по случайному совпадению букв.
BALANCE = 0.6
#: Короче этого фразы не разбираем: у команды из одного слова ослышаться не в
#: чем — подменится она целиком.
LEAST_WORDS = 2
#: Длина слова, которое годится в опору. Совпавшие служебные слова ничего не
#: значат: «что с интернетом» и «что с эквалайзером» сходятся на «что» и «с»,
#: а «как дела» — на одном «как». Стенд на 214 живых репликах: без этого
#: правила три ложных находки из шести, с ним — ни одной.
LEAST_ANCHOR = 4
#: Уверенность найденного. Ниже точной фразы и шаблона, выше порога роутера.
CONFIDENCE = 0.7


def _same(left: str, right: str) -> bool:
    """Одно и то же слово: буква в букву, по основе или на слух."""
    return left == right or stem(left) == stem(right) or sounds_alike(left, right)


def misheard(said: list[str], phrase: list[str], known: frozenset[str] = frozenset()) -> float:
    """Похожесть реплики на фразу, если разница ровно в одном слове.

    :param known: основы всех слов, встречающихся в командах системы. Слово
        оттуда ослышкой не считается: «открой телеграм» отличается от «открой
        телефон» одним словом, но это **другая команда**, а не ослышка, и
        подменять одну другой нельзя. Тот же случай — «закрой» и «открой».
    :return: похожесть ослышанного слова; ``0`` — это не ослышка.
    """
    if len(said) != len(phrase) or len(phrase) < LEAST_WORDS:
        return 0.0
    odd: tuple[str, str] | None = None
    for mine, theirs in zip(said, phrase, strict=True):
        if _same(mine, theirs):
            continue
        if odd is not None:
            return 0.0
        odd = (mine, theirs)
    if odd is None:
        return 0.0  # совпало целиком — это работа точных фраз, не наша
    anchors = [word for word, theirs in zip(said, phrase, strict=True) if word != odd[0]]
    if not any(len(word) >= LEAST_ANCHOR for word in anchors):
        return 0.0
    if stem(odd[0]) in known or any(word.startswith(stem(odd[0])[:LEAST_ANCHOR]) for word in known):
        return 0.0
    score = closeness(*odd, balance=BALANCE)
    return score if score >= THRESHOLD else 0.0


class SimilarResolver:
    """Команда, не узнанная из-за ослышки в одном слове."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        """Имя резолвера."""
        return "similar"

    def _phrases(self) -> tuple[list[tuple[list[str], str, str]], frozenset[str]]:
        """Фразы без слотов (слова, фраза, инструмент) и словарь всех слов команд."""
        found: list[tuple[list[str], str, str]] = []
        known: set[str] = set()
        for spec in self._registry.specs():
            for phrase in spec.phrases:
                normalized = " ".join(phrase.lower().split())
                words = normalized.split()
                # Словарь собирается и по шаблонам тоже: «поставь пресет {name}»
                # учит нас словам «поставь» и «пресет», и подменять их нельзя.
                known.update(stem(word) for word in words if not word.startswith("{"))
                if _compile(normalized) is not None:
                    continue
                if len(words) >= LEAST_WORDS:
                    found.append((words, normalized, spec.name))
        return found, frozenset(known)

    async def resolve(self, utterance: Utterance) -> Intent | None:
        """Найти команду, от которой реплика отличается одним словом."""
        said = utterance.normalized.split()
        if len(said) < LEAST_WORDS:
            return None

        best: tuple[float, str, str] | None = None
        phrases, known = self._phrases()
        for words, phrase, tool_name in phrases:
            score = misheard(said, words, known)
            if score and (best is None or score > best[0]):
                best = (score, phrase, tool_name)
        if best is None:
            return None

        score, phrase, tool_name = best
        logger.info(
            "Похоже на команду: %r ≈ %r (%.2f) -> %s", utterance.normalized, phrase, score, tool_name
        )
        return Intent(
            tool=tool_name,
            confidence=CONFIDENCE,
            resolver=self.name,
            utterance=utterance.text,
        )
