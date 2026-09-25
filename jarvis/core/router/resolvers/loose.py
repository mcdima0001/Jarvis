"""Команда с лишними словами внутри: «поставь **в** таймер на 5 минут».

Живой случай 23.09.2026: «поставь таймер на 5 минут» сработало, а «поставь **в**
таймер на 5 минут» ушло в свободный разговор — разница в двух буквах, а результат
противоположный. Замысел владельца: фраза должна разбиваться на опорные слова,
между которыми допускается мусор, — «поставь … таймер … 5 … минут».

**«Что угодно между словами» — это ровно то, на чём проект уже горел**, поэтому
правило ограничено четырьмя условиями, и каждое куплено чужой бедой:

1. **Первое слово команды обязано стоять первым.** Без этого «напиши маме
   поставь таймер на пять минут» перестаёт быть диктовкой и становится
   таймером: слова команды живут и внутри обычной речи.
2. **Между опорами не больше `MAX_GAP` чужих слов.** Иначе длинная фраза
   находит себе команду по случайно разбросанным словам — так шаблон
   «найди {query}» однажды забирал весь хвост чужой просьбы.
3. **Отрицание и отмена в промежутке запрещают совпадение.** «Поставь чайник, а
   таймер не надо» не должно ставить таймер: цена ошибки тут односторонняя.
4. **Опор должно быть две и не короче восьми букв вместе.** Одно слово — это не
   узнавание команды, а угадывание: «таймер» само по себе ловится точной фразой.

Стоит **после** точных фраз, диктовки и выученного: всё, что разбирается
строго, разбирается строго. Ловит ровно то, что иначе ушло бы в облако.
"""

from __future__ import annotations

import logging
import re

from jarvis.core.contracts import Intent, Utterance
from jarvis.core.text.matching import sounds_alike, stem
from jarvis.core.tools import ToolRegistry

from ..templates import second_request

logger = logging.getLogger(__name__)

#: Сколько чужих слов допускается между двумя опорами. Четыре — это замер:
#: «поставь ты уже блин этот таймер» владелец привёл как пример того, что
#: обязано работать, а в нём между опорами ровно четыре лишних слова.
MAX_GAP = 4
#: Сколько мусора допускается во всей фразе. Отдельно от `MAX_GAP`: одно
#: длинное вклинивание — это оговорка, а мусор в каждом промежутке — другая
#: фраза, случайно составленная из тех же слов.
MAX_JUNK = 4
#: Сколько опорных слов обязано совпасть и сколько в них букв.
LEAST_ANCHORS = 2
LEAST_LETTERS = 8
#: Слова, которые в промежутке отменяют смысл команды.
STOPPERS = frozenset({
    "не", "ни", "без", "нет", "нельзя", "хватит", "перестань", "отмени", "убери",
    "no", "not", "don't", "dont", "cancel", "stop",
})
#: Уверенность: ниже точной фразы и шаблона, выше разбора моделью.
CONFIDENCE = 0.8

#: Подстановка в объявленной фразе.
SLOT = re.compile(r"^\{([a-zA-Z_][\w]*)\}$")


#: Короче этого слова сравниваются только буква в букву.
#:
#: Иначе служебные слова слипаются: `stem` отбрасывает гласные на конце, и у
#: «на» с «не» остаётся одно и то же «н». На этом «поставь чайник, а таймер НЕ
#: надо» совпало с «поставь таймер НА {request}» и поставило таймер (поймано
#: 23.09.2026, в первый же час жизни резолвера).
LEAST_STEM = 3


def _same(left: str, right: str) -> bool:
    """Одно и то же слово: буква в букву, по основе или на слух."""
    if left == right:
        return True
    if min(len(left), len(right)) < LEAST_STEM:
        return False
    return (stem(left) and stem(left) == stem(right)) or sounds_alike(left, right)


def match(said: list[str], parts: list[str]) -> tuple[dict[str, str], int] | None:
    """Разобрать реплику по опорным словам фразы.

    :param said: слова реплики в нижнем регистре.
    :param parts: слова объявленной фразы; подстановки — как ``{имя}``.
    :return: пара «аргументы, сколько чужих слов пропущено» либо ``None``.
    """
    anchors = [word for word in parts if not SLOT.match(word)]
    if len(anchors) < LEAST_ANCHORS or sum(len(word) for word in anchors) < LEAST_LETTERS:
        return None

    found: dict[str, str] = {}
    at = 0          # куда дошли в реплике
    junk = 0        # сколько чужих слов пропустили
    waiting = ""    # подстановка, ждущая своего значения
    collected: list[str] = []

    for part in parts:
        slot = SLOT.match(part)
        if slot is not None:
            waiting = slot.group(1)
            collected = []
            continue

        # Ищем опору впереди: подстановка глотает всё до неё, а простой
        # промежуток ограничен MAX_GAP словами.
        limit = len(said) if waiting else min(len(said), at + MAX_GAP + 1)
        step = at
        while step < limit and not _same(said[step], part):
            # Отрицание запрещает совпадение и внутри промежутка, и внутри
            # подстановки: «поставь чайник, а таймер не надо» — это не таймер.
            if said[step] in STOPPERS:
                return None
            collected.append(said[step])
            step += 1
        if step >= limit:
            return None
        if waiting:
            if not collected:
                return None
            found[waiting] = " ".join(collected)
            waiting = ""
        else:
            junk += len(collected)
        collected = []
        at = step + 1

    tail = said[at:]
    if any(word in STOPPERS for word in tail):
        return None
    if waiting:
        if not tail:
            return None
        found[waiting] = " ".join(tail)
    elif len(tail) > MAX_GAP:
        # Хвост длиннее промежутка — это уже другая фраза, а не лишнее словечко.
        return None
    else:
        junk += len(tail)
    return (found, junk) if junk <= MAX_JUNK else None


class LooseResolver:
    """Объявленная команда, в которую человек вставил лишние слова."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        """Имя резолвера."""
        return "loose"

    async def resolve(self, utterance: Utterance) -> Intent | None:
        """Найти команду, от которой реплика отличается только мусором внутри."""
        said = utterance.normalized.split()
        if len(said) < LEAST_ANCHORS:
            return None

        best: tuple[int, int, dict[str, str], str, str] | None = None
        for spec in self._registry.specs():
            for phrase in spec.phrases:
                parts = phrase.lower().split()
                if not parts or not _same(said[0], parts[0].strip("{}")):
                    # Команда обязана начинаться с первого же слова реплики.
                    continue
                got = match(said, parts)
                if got is None:
                    continue
                arguments, junk = got
                if second_request(arguments):
                    # Вторая просьба в слоте: «канал veritasium и открой его».
                    continue
                if not junk:
                    # Хоть одна фраза совпала начисто — значит это работа точных
                    # фраз, а не наша. Молчим целиком, иначе «поставь таймер на
                    # 5 минут» досталось бы короткой фразе «поставь таймер», и
                    # длительность потерялась бы в «мусоре» (та самая беда
                    # 01.08.2026: таймер на минуту превратился в пять).
                    return None
                weight = sum(len(word) for word in parts if not SLOT.match(word))
                if best is None or (-junk, weight) > (-best[1], best[0]):
                    best = (weight, junk, arguments, phrase, spec.name)
        if best is None:
            return None

        _, junk, arguments, phrase, tool_name = best
        logger.info(
            "Команда с лишними словами: %r ≈ %r (мусора %d) -> %s",
            utterance.normalized, phrase, junk, tool_name,
        )
        return Intent(
            tool=tool_name,
            arguments=arguments,
            confidence=CONFIDENCE,
            resolver=self.name,
            utterance=utterance.text,
        )
