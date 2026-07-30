"""Манера речи: чем ассистент отвечает, когда своих слов у команды нет.

Голосовой помощник произносит одни и те же служебные реплики десятки раз в
день. Одна зашитая строка («Слушаю.») за неделю превращается в сигнал
будильника: её перестают слышать. Поэтому отклики живут наборами вариантов, и
подряд один и тот же не повторяется.

Наборы собраны здесь, а не размазаны по конвейеру и скиллам, потому что
характер — это одно решение, а не двадцать. Персона отвечает там, где сказать
нечего: позвали по имени, команда выполнена, команда не вышла, запуск,
остановка.

Свои слова у команды остаются («Запускаю Steam»), но **выбирать из вариантов —
тоже работа персоны** (:meth:`Persona.choose`). Скилл перечисляет, чем можно
ответить, а «не повторяться» и «помнить, что уже сказал» — одна забота на всю
систему, а не по копии в каждом скилле. Память своя на каждый ключ: сказанная
«пауза» не должна вытеснять «включаю».

Обращение вынесено в поле ``{address}``: в фильме это «сэр», но заменить его
на имя или убрать совсем — правка одной строки конфига, а не всех фраз. Если
обращение пустое, из шаблона убирается и оно, и запятая перед ним, иначе
получилось бы «Слушаю, .».
"""

from __future__ import annotations

import logging
import random
import re
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

logger = logging.getLogger(__name__)

#: Ситуации, на которые у ассистента есть свои слова.
LISTENING = "listening"   # позвали по имени и замолчали
DONE = "done"             # команда выполнена, своей реплики у неё нет
FAILED = "failed"         # команда не выполнилась и не объяснила почему
GREETING = "greeting"     # запуск
FAREWELL = "farewell"     # остановка

SITUATIONS: tuple[str, ...] = (LISTENING, DONE, FAILED, GREETING, FAREWELL)

#: Как ассистент обращается к владельцу. Пустая строка убирает обращение.
DEFAULT_ADDRESS: Mapping[str, str] = {"ru": "сэр", "en": "sir"}

#: Приветствие по времени суток: часы, с которых оно начинается.
_DAYPARTS: tuple[tuple[int, str, str], ...] = (
    (5, "Доброе утро", "Good morning"),
    (12, "Добрый день", "Good afternoon"),
    (18, "Добрый вечер", "Good evening"),
    (23, "Доброй ночи", "Good evening"),
)

#: Встроенные наборы. Порядок внутри набора не важен — выбор случайный.
PHRASES: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    LISTENING: {
        "ru": (
            "Слушаю, {address}.",
            "Да, {address}?",
            "К вашим услугам, {address}.",
            "Я здесь, {address}.",
            "Я весь внимание, {address}.",
            "Слушаю вас.",
            "Чем могу помочь, {address}?",
            "На связи, {address}.",
            "Готов служить, {address}.",
            "Что для вас сделать, {address}?",
            "Внимательно слушаю.",
            "Слушаю, {address}. Говорите.",
            "Да, {address}. Я тут.",
            "Весь ваш, {address}.",
            "Слушаю внимательно, {address}.",
            "Я на месте, {address}.",
            "Говорите, {address}.",
            "К вашим услугам.",
        ),
        "en": (
            "Yes, {address}?",
            "At your service, {address}.",
            "I'm listening, {address}.",
            "Standing by, {address}.",
            "Right here, {address}.",
            "Go ahead, {address}.",
            "How can I help, {address}?",
            "Ready when you are, {address}.",
            "Listening.",
            "You have my attention, {address}.",
            "Yes? I'm here.",
            "At once, {address}. Go ahead.",
            "I'm all ears, {address}.",
            "Here, {address}.",
            "Whenever you're ready, {address}.",
            "Awaiting instructions, {address}.",
        ),
    },
    DONE: {
        "ru": (
            "Готово, {address}.",
            "Сделано.",
            "Выполнено, {address}.",
            "Как скажете, {address}.",
            "Есть, {address}.",
            "Уже сделано, {address}.",
            "Разумеется, {address}.",
            "Считайте, что сделано.",
            "Всё в порядке, {address}.",
            "Готово.",
            "Исполнено, {address}.",
            "Сию минуту, {address}.",
            "Разумеется. Готово.",
            "Принято, {address}.",
            "Уже, {address}.",
            "С удовольствием, {address}.",
            "Как пожелаете.",
            "Сделал, {address}.",
            "Не вопрос, {address}.",
            "Всё готово.",
        ),
        "en": (
            "Done, {address}.",
            "Consider it done.",
            "Right away, {address}.",
            "As you wish, {address}.",
            "Taken care of, {address}.",
            "It's done, {address}.",
            "Of course, {address}.",
            "All set.",
            "Certainly, {address}.",
            "Done.",
            "At once, {address}.",
            "Already handled, {address}.",
            "With pleasure, {address}.",
            "Noted, {address}.",
            "That's done.",
            "Very good, {address}.",
            "As requested, {address}.",
            "Complete.",
        ),
    },
    FAILED: {
        "ru": (
            "Не вышло, {address}.",
            "Боюсь, не получилось, {address}.",
            "Не справился, {address}.",
            "Тут я бессилен, {address}.",
            "Команда не прошла, {address}.",
            "К сожалению, ничего не вышло.",
            "Что-то пошло не так, {address}.",
            "Не смог выполнить, {address}.",
            "Увы, {address}.",
            "Не получилось, {address}. Попробуем иначе?",
            "Боюсь, это выше моих сил.",
            "Ничего не вышло, {address}.",
            "Не сработало, {address}.",
            "Здесь я вам не помощник, {address}.",
            "Не удалось, {address}.",
            "Пока не выходит, {address}.",
        ),
        "en": (
            "That didn't work, {address}.",
            "I'm afraid it failed, {address}.",
            "I couldn't do that, {address}.",
            "No luck, {address}.",
            "The command didn't go through, {address}.",
            "Something went wrong, {address}.",
            "I'm afraid not, {address}.",
            "Unable to do that.",
            "Afraid that's beyond me, {address}.",
            "That one got away from me, {address}.",
            "It didn't take, {address}.",
            "No joy, {address}.",
            "Not this time, {address}.",
            "I couldn't manage it, {address}.",
        ),
    },
    GREETING: {
        "ru": (
            "{greeting}, {address}. Все системы в норме.",
            "{greeting}, {address}. Я в вашем распоряжении.",
            "{greeting}, {address}. Слушаю.",
            "{greeting}, {address}. Системы запущены.",
            "{greeting}, {address}. Готов к работе.",
            "{greeting}, {address}. Я в сети.",
            "{greeting}, {address}. Все службы поднялись.",
            "{greeting}, {address}. Жду распоряжений.",
            "{greeting}, {address}. Как обычно, к вашим услугам.",
            "{greeting}, {address}. Я на связи.",
        ),
        "en": (
            "{greeting}, {address}. All systems are online.",
            "{greeting}, {address}. I'm at your disposal.",
            "{greeting}, {address}. Standing by.",
            "{greeting}, {address}. Systems are up.",
            "{greeting}, {address}. Ready to work.",
            "{greeting}, {address}. Everything's running.",
            "{greeting}, {address}. Awaiting your orders.",
            "{greeting}, {address}. Back online.",
        ),
    },
    FAREWELL: {
        "ru": (
            "До связи, {address}.",
            "Отключаюсь, {address}.",
            "Всего доброго, {address}.",
            "Ухожу в спящий режим, {address}.",
            "Работу завершаю, {address}.",
            "До скорого, {address}.",
            "Всё выключаю, {address}.",
            "Буду нужен — позовите, {address}.",
            "Хорошего дня, {address}.",
            "Ухожу, {address}.",
        ),
        "en": (
            "Goodbye, {address}.",
            "Shutting down, {address}.",
            "Until next time, {address}.",
            "Going offline, {address}.",
            "Powering down, {address}.",
            "Call me when you need me, {address}.",
            "Signing off, {address}.",
            "Have a good one, {address}.",
        ),
    },
}

#: Характер для языковой модели. Уходит только в свободный диалог
#: (задача ``dialog``), где ответ сочиняет она, а не скилл.
_STYLE: Mapping[str, str] = {
    "ru": (
        "Держись как Джарвис из фильмов о Железном человеке: безупречно вежлив, "
        "невозмутим, говоришь коротко и по существу. Уместна сдержанная сухая "
        "ирония. Без извинений, подобострастия и болтовни."
    ),
    "en": (
        "Behave like Jarvis from the Iron Man films: impeccably polite, unflappable, "
        "brief and to the point. Dry understated wit is welcome. No apologies, "
        "no fawning, no small talk."
    ),
}

#: Отдельным предложением: без обращения его быть не должно вовсе.
_STYLE_ADDRESS: Mapping[str, str] = {
    "ru": "Обращайся к собеседнику «{address}» — к месту, а не в каждой фразе.",
    "en": "Address the user as \"{address}\" — where it fits, not in every sentence.",
}

#: Обращение вместе с запятой при нём: убираются только вместе, иначе от
#: «Слушаю, {address}.» осталось бы «Слушаю, .».
_ADDRESS_SLOT = re.compile(
    r"\s*,\s*\{address\}"      # «Слушаю, {address}»
    r"|\{address\}\s*,\s*"     # «{address}, слушаю»
    r"|\s*\{address\}"         # обращение само по себе
)


def daypart(language: str = "ru", *, now: datetime | None = None) -> str:
    """Приветствие по времени суток: «Доброе утро», «Добрый вечер»…"""
    hour = (now or datetime.now()).hour
    english = language.startswith("en")
    greeting = "Доброй ночи" if not english else "Good evening"
    for start, russian, other in _DAYPARTS:
        if hour >= start:
            greeting = other if english else russian
    return greeting


def _fill(template: str, fields: Mapping[str, str]) -> str:
    """Подставить поля в шаблон, аккуратно убрав пустое обращение."""
    text = template if fields.get("address") else _ADDRESS_SLOT.sub("", template)
    try:
        return text.format(**fields).strip()
    except (KeyError, IndexError) as exc:
        # Своя фраза из конфига с незнакомой подстановкой. Ронять из-за этого
        # разговор незачем — произнесём как есть и скажем, где искать.
        logger.warning("В реплике %r неизвестная подстановка %s", template, exc)
        return text


class Persona:
    """Служебные реплики ассистента: варианты, обращение, характер.

    :param phrases: свои наборы, ситуация -> язык -> список фраз.
    :param address: обращение по языкам; ключ ``*`` — для всех сразу.
    :param replace: ``True`` — свои наборы вместо встроенных, иначе в дополнение.
    :param default_language: чей набор брать, если для языка фраз нет.
    :param choice: выбор варианта; подменяется в тестах.
    """

    def __init__(
        self,
        *,
        phrases: Mapping[str, Mapping[str, Sequence[str]]] | None = None,
        address: Mapping[str, str] | None = None,
        replace: bool = False,
        default_language: str = "ru",
        greet_on_start: bool = True,
        farewell_on_stop: bool = True,
        choice: Callable[[Sequence[str]], str] = random.choice,
    ) -> None:
        self._phrases = self._merge(phrases or {}, replace=replace)
        self._address = {**DEFAULT_ADDRESS, **(address or {})}
        self._default = _code(default_language)
        self.greet_on_start = greet_on_start
        self.farewell_on_stop = farewell_on_stop
        self._choice = choice
        #: Что уже говорили: чтобы вариант не повторился дважды подряд.
        self._recent: dict[tuple[str, str], deque[str]] = {}

    @staticmethod
    def _merge(
        extra: Mapping[str, Mapping[str, Sequence[str]]],
        *,
        replace: bool,
    ) -> dict[str, dict[str, tuple[str, ...]]]:
        """Свести встроенные наборы с теми, что задал владелец."""
        merged = {
            situation: dict(languages) for situation, languages in PHRASES.items()
        }
        for situation, languages in extra.items():
            if situation not in merged:
                # Опечатка в названии ситуации иначе осталась бы незамеченной:
                # фразы просто никогда бы не прозвучали.
                logger.warning(
                    "persona.phrases: неизвестная ситуация %r — пропускаю. Есть: %s",
                    situation,
                    ", ".join(SITUATIONS),
                )
                continue
            for code, items in languages.items():
                lines = tuple(str(item).strip() for item in items if str(item).strip())
                if not lines:
                    continue
                current = () if replace else merged[situation].get(_code(code), ())
                merged[situation][_code(code)] = (*current, *lines)
        return merged

    def address_for(self, language: str | None = None) -> str:
        """Обращение на нужном языке (``*`` перекрывает все языки)."""
        code = _code(language or self._default)
        return self._address.get("*", self._address.get(code, ""))

    def variants(self, situation: str, language: str | None = None) -> tuple[str, ...]:
        """Все варианты для ситуации — для отчёта ``--check`` и тестов."""
        languages = self._phrases.get(situation, {})
        code = _code(language or self._default)
        return languages.get(code) or languages.get(self._default) or ()

    def line(self, situation: str, language: str | None = None, **fields: str) -> str:
        """Выбрать реплику, не повторяя недавние.

        :param situation: одна из :data:`SITUATIONS`.
        :param language: язык реплики; при отсутствии набора берётся основной.
        :return: готовый текст либо пустая строка, если вариантов нет.
        """
        pool = self.variants(situation, language)
        if not pool:
            return ""
        code = _code(language or self._default)
        template = self._pick(situation, code, pool)
        return _fill(
            template,
            {
                "address": self.address_for(code),
                "greeting": daypart(code),
                **fields,
            },
        )

    def choose(
        self, key: str, variants: Sequence[str], language: str | None = None
    ) -> str:
        """Выбрать один из вариантов чужой реплики, не повторяя недавние.

        Нужно скиллам: «Пауза.» звучит десятки раз в день, и одна зашитая
        строка на слух превращается в сигнал будильника. Свои слова у команды
        есть — варьировать их персона всё равно обязана, потому что «не
        повторяться» и «помнить, что уже сказал» — это ровно её работа, а не
        работа каждого скилла по отдельности.

        :param key: чья это реплика; обычно имя инструмента. Своя память на
            каждый ключ: «пауза» не должна вытеснять «включаю».
        """
        pool = tuple(str(item).strip() for item in variants if str(item).strip())
        if not pool:
            return ""
        return self._pick(key, _code(language or self._default), pool)

    def _pick(self, situation: str, code: str, pool: tuple[str, ...]) -> str:
        """Взять вариант, которого давно не было.

        Помним половину набора: при двенадцати откликах повтор возможен не
        раньше седьмого обращения — на слух это уже воспринимается как живая
        речь, а не как список.
        """
        key = (situation, code)
        recent = self._recent.get(key)
        if recent is None:
            recent = self._recent.setdefault(key, deque(maxlen=max(1, len(pool) // 2)))
        fresh = [item for item in pool if item not in recent] or list(pool)
        chosen = self._choice(fresh)
        recent.append(chosen)
        return chosen

    def style(self, language: str | None = None) -> str:
        """Описание характера для системной подсказки языковой модели."""
        code = _code(language or self._default)
        if code not in _STYLE:
            code = "ru"
        style = _STYLE[code]
        address = self.address_for(code)
        if address:
            style = f"{style} {_STYLE_ADDRESS[code].format(address=address)}"
        return style

    def summary(self) -> str:
        """Строка для отчёта ``--check``: обращение и размеры наборов."""
        address = self.address_for() or "нет"
        counts = ", ".join(
            f"{situation} {len(self.variants(situation))}" for situation in SITUATIONS
        )
        return f"обращение «{address}», реплик: {counts}"


def _code(language: str | None) -> str:
    """Привести код языка к короткому виду: ``ru-RU`` -> ``ru``."""
    return (language or "ru").split("-")[0].lower()
