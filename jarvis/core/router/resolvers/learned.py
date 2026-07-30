"""Выученные формулировки — кеш удачных разборов моделью.

Модель разобрала фразу, инструмент отработал успешно — значит связка «эти слова
означают эту команду» проверена делом. Записываем её в память, и со второго раза
та же формулировка обрабатывается бесплатно и мгновенно, как если бы её с самого
начала объявили в скилле.

Экономия тут не косметическая. Разбор моделью тащит с собой весь каталог
инструментов — под три тысячи входных токенов на фразу, — и повторяется он на
каждой одинаковой реплике. Живая речь же однообразна: человек говорит «заблокируй
ноутбук» одними и теми же словами месяцами.

**Записывается только то, что сработало.** Ошибочный разбор в память попасть не
должен: закрепить промах хуже, чем не выучить ничего. Поэтому решение принимает
диспетчер — единственное место, где известны и намерение, и результат вызова.

**Обобщение — осторожное.** Если значение аргумента слышно в самой реплике
(«загугли рецепт борща» → ``query="рецепт борща"``), из фразы можно сделать
шаблон и выучить сразу целое семейство. Но шаблон, выведенный автоматически,
легко оказывается слишком широким: «включи {control}» поймает и «включи свет».
Поэтому требований два — не меньше двух собственных слов и не меньше восьми
собственных букв. Не прошло — запоминаем фразу целиком, как услышали.

Проверяется этот резолвер **после** объявленных фраз и синонимов: то, что
написано в скилле руками, всегда главнее выученного, которое могло и устареть.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping

from jarvis.core.contracts import Intent, Utterance
from jarvis.core.memory import DocumentStore

from ..templates import PLACEHOLDER, compile_template, literal_words, specificity

logger = logging.getLogger(__name__)

#: Раздел памяти по умолчанию.
SECTION = "commands"

#: Инструменты, которые запоминать бессмысленно или вредно.
#:
#: ``core.chat`` — это и есть «не понял», его запоминание закрепило бы отказ.
#: ``core.forget_last`` отменяет запись; выучить его — значит записать в память
#: команду забывания, которую потом придётся забывать.
NEVER_LEARN = frozenset({"core.chat", "core.forget_last"})

#: Сколько своих слов и букв обязан сохранить выведенный шаблон.
MIN_TEMPLATE_WORDS = 2
MIN_TEMPLATE_LETTERS = 8

#: Короче этого фраза не запоминается: «да», «нет», «ок» ничего не значат.
MIN_UTTERANCE = 6


def normalize(text: str) -> str:
    """Привести реплику к виду, в котором она хранится и сравнивается."""
    return " ".join(str(text).lower().split()).strip(" .,!?;:«»\"'")


def generalize(utterance: str, arguments: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Превратить реплику в шаблон, если аргументы слышны в ней самой.

    :return: пара «ключ памяти, аргументы». В аргументах на месте вытащенных из
        речи значений стоит подстановка ``{имя}`` — при совпадении шаблона она
        заменяется тем, что услышано в этот раз.
    """
    key = normalize(utterance)
    stored: dict[str, Any] = dict(arguments)
    if not key:
        return key, stored

    for name, value in arguments.items():
        text = normalize(value) if isinstance(value, str) else ""
        # Слишком короткое значение сравнивать бессмысленно: «на» найдётся
        # в любой фразе и превратит её в шаблон из одного слова.
        if len(text) < 4 or text not in key or f"{{{name}}}" in key:
            continue
        candidate = key.replace(text, f"{{{name}}}", 1)
        if (
            literal_words(candidate) >= MIN_TEMPLATE_WORDS
            and specificity(candidate) >= MIN_TEMPLATE_LETTERS
        ):
            key = candidate
            stored[name] = f"{{{name}}}"

    return key, stored


def fill(arguments: Mapping[str, Any], found: Mapping[str, str]) -> dict[str, Any]:
    """Подставить услышанное в аргументы шаблона."""
    result: dict[str, Any] = {}
    for name, value in arguments.items():
        if isinstance(value, str):
            match = PLACEHOLDER.fullmatch(value.strip())
            if match is not None:
                heard = found.get(match.group(1))
                if heard is None:
                    return {}
                result[name] = heard.strip()
                continue
        result[name] = value
    return result


class LearnedResolver:
    """Формулировки, выученные из удачных разборов моделью.

    Один объект и читает, и пишет: это две стороны одного дела — кеша ответов
    модели. Хранилище — документ памяти, поэтому выученное переживает
    перезапуск.
    """

    def __init__(
        self,
        store: DocumentStore,
        *,
        section: str = SECTION,
        enabled: bool = True,
    ) -> None:
        self._store = store
        self._section = section
        self._enabled = enabled
        #: ``None`` — из памяти ещё не читали.
        self._known: dict[str, dict[str, Any]] | None = None
        #: Что записали последним: это и отменяет «не сохраняй в память».
        self._last: str = ""

    @property
    def name(self) -> str:
        """Имя резолвера."""
        return "learned"

    @property
    def last_learned(self) -> str:
        """Последняя выученная формулировка за этот сеанс."""
        return self._last

    async def _load(self) -> dict[str, dict[str, Any]]:
        """Прочитать выученное; недоступный раздел выключает обучение."""
        if self._known is not None:
            return self._known
        try:
            raw = await self._store.read(self._section)
        except Exception as exc:  # noqa: BLE001 — без памяти работаем как раньше
            logger.warning(
                "Раздел памяти %r недоступен (%s) — формулировки не запоминаются. "
                "Проверь, что он объявлен в memory.documents",
                self._section,
                exc,
            )
            self._enabled = False
            self._known = {}
            return self._known

        # В записи бывает выученный инструмент, бывает список отвергнутых, а
        # бывает и то и другое: «эти слова означают вот это, а вот это уже
        # пробовали».
        self._known = {
            str(key): dict(value)
            for key, value in raw.items()
            if isinstance(value, Mapping) and (value.get("tool") or value.get("rejected"))
        }
        if self._known:
            logger.info("Выученных формулировок в памяти: %d", len(self._known))
        return self._known

    async def resolve(self, utterance: Utterance) -> Intent | None:
        """Найти выученную формулировку — точную или шаблонную."""
        known = await self._load()
        if not known:
            return None
        text = normalize(utterance.text)
        if not text:
            return None

        entry = known.get(text)
        key = text
        arguments: Mapping[str, Any] = {}
        if entry is not None and not entry.get("tool"):
            # Запись есть, но она про отвергнутое — подсказать нечего.
            return None
        if entry is not None:
            arguments = dict(entry.get("arguments") or {})
        else:
            key, entry, arguments = self._match(known, utterance.cleaned)
            if entry is None:
                return None

        logger.info(
            "Формулировка %r уже выучена — %s без обращения к модели",
            utterance.text,
            entry["tool"],
        )
        # Отменять просят последнее сделанное, а не последнее выученное: чаще
        # всего мимо бьёт как раз то, что выучено вчера и повторилось сегодня.
        self._last = key
        return Intent(
            tool=str(entry["tool"]),
            arguments=arguments,
            # Ниже точной фразы, выше разбора моделью: связка проверена делом,
            # но написана не человеком.
            confidence=0.9,
            resolver=self.name,
            utterance=utterance.text,
        )

    def _match(
        self, known: Mapping[str, Mapping[str, Any]], text: str
    ) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
        """Подобрать шаблон: от частного к общему, как и объявленные фразы."""
        scored: list[tuple[int, str, re.Pattern[str]]] = []
        for key in known:
            pattern = compile_template(key)
            if pattern is not None:
                scored.append((specificity(key), key, pattern))
        scored.sort(key=lambda item: item[0], reverse=True)

        for _, key, pattern in scored:
            match = pattern.match(text)
            if match is None:
                continue
            entry = dict(known[key])
            if not entry.get("tool"):
                continue
            arguments = fill(entry.get("arguments") or {}, match.groupdict())
            if not arguments and (entry.get("arguments") or {}):
                # Шаблон совпал, а подставить нечего — запись битая.
                logger.debug("Выученный шаблон %r не удалось заполнить", key)
                continue
            return key, entry, arguments
        return "", None, {}

    # --- запись ------------------------------------------------------------

    async def remember(self, utterance: str, intent: Intent) -> str:
        """Запомнить удачный разбор.

        :return: ключ, под которым записали, либо пустую строку.
        """
        if not self._enabled or intent.tool in NEVER_LEARN:
            return ""
        known = await self._load()
        if not self._enabled:
            return ""

        key, arguments = generalize(utterance, intent.arguments)
        if len(key) < MIN_UTTERANCE:
            return ""

        previous = known.get(key) or {}
        rejected = [str(name) for name in (previous.get("rejected") or ()) if name]
        if intent.tool in rejected:
            # Это уже пробовали, и владелец сказал «не то». Модель предложила
            # то же самое второй раз — запоминать нельзя, иначе отмена
            # бессмысленна: связка выучится снова.
            logger.info(
                "Формулировку %r с %s уже отвергали — не запоминаю", key, intent.tool
            )
            return ""

        entry: dict[str, Any] = {"tool": intent.tool, "arguments": arguments}
        if rejected:
            entry["rejected"] = rejected
        if previous == entry:
            # Уже выучено — второй раз сюда попадаем, только если фраза дошла
            # до модели в обход выученного. Записывать нечего.
            return ""

        try:
            await self._store.set(self._section, key, entry)
        except Exception as exc:  # noqa: BLE001 — не записалось, но команда выполнена
            logger.warning("Не смог запомнить формулировку %r: %s", key, exc)
            return ""

        known[key] = entry
        self._last = key
        logger.info(
            "Запомнил формулировку: %r -> %s%s. Не то, что нужно — скажи «не сохраняй в память»",
            key,
            intent.tool,
            f" {arguments}" if arguments else "",
        )
        return key

    async def rejected_for(self, utterance: str) -> tuple[str, ...]:
        """Инструменты, которые для этой просьбы уже оказались не теми.

        Нужно модели: показать ей то, что уже пробовали, дешевле, чем ждать,
        пока она сама предложит другое.
        """
        known = await self._load()
        text = normalize(utterance)
        entry = known.get(text)
        if entry is None:
            for key in known:
                pattern = compile_template(key)
                if pattern is not None and pattern.match(text):
                    entry = known[key]
                    break
        if entry is None:
            return ()
        return tuple(str(name) for name in (entry.get("rejected") or ()) if name)

    async def reject(self, key: str = "") -> str:
        """Отменить выученную формулировку и запомнить, что она была не той.

        Отмена без памяти о причине бесполезна: модель предложила бы то же
        самое, разбор прошёл бы «удачно», и формулировка выучилась бы снова. А
        так следующий разбор идёт с оговоркой «это уже пробовали».

        :return: что забыли, либо пустую строку.
        """
        known = await self._load()
        target = normalize(key) if key else self._last
        entry = known.get(target) if target else None
        if not target or entry is None:
            return ""

        wrong = str(entry.get("tool") or "")
        rejected = [str(name) for name in (entry.get("rejected") or ()) if name]
        if wrong and wrong not in rejected:
            rejected.append(wrong)

        data = dict(known)
        if rejected:
            # Запись остаётся, но уже как «эти слова — не про это».
            data[target] = {"rejected": rejected}
        else:
            data.pop(target, None)

        try:
            await self._store.write(self._section, data)
        except Exception as exc:  # noqa: BLE001 — забыть не вышло, но это не сбой команды
            logger.warning("Не смог забыть формулировку %r: %s", target, exc)
            return ""

        self._known = {
            name: dict(value) for name, value in data.items() if isinstance(value, Mapping)
        }
        if target == self._last:
            self._last = ""
        logger.info(
            "Забыл формулировку %r%s",
            target,
            f" и больше не предложу {wrong}" if wrong else "",
        )
        return target
