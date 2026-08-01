"""Режимы — то немногое, что живёт между командами.

До сих пор у ассистента было ровно одно состояние: окно ответа после имени.
Всё остальное он забывал сразу же, поэтому просьбы вида «полчаса не слушай»,
«отвечай покороче» выразить было нечем — это не действия, а **поведение на
время**.

Устройство намеренно скучное: флаг с именем и сроком жизни. Никакого тика и
никакого хранилища — и это не экономия, а следствие. Режим никто не поджигает:
его достаточно проверить в тот момент, когда его читают, а читают его на каждой
реплике. Часы (то есть отдельный планировщик с переживанием перезапуска) нужны
другому классу просьб — «напомни через час», — где проверять некому.

Перезапуск режимы не переживают, и это тоже осознанно: «не слушай полчаса»
после перезагрузки ассистента звучало бы издевательством.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Не принимать команды вовсе. Единственный режим, который сам себя защищает:
#: пока он включён, до роутера доходят только фразы пробуждения (см. ниже).
DEAF = "deaf"
#: Отвечать короче обычного. Меняет системную подсказку свободного разговора.
BRIEF = "brief"

#: Как режим называется вслух и в подсказке модели. Имя вроде `deaf` человеку
#: не говорят, а модели без расшифровки оно ничего не сообщает.
_TITLES: dict[str, dict[str, str]] = {
    "ru": {DEAF: "не слушаю команды", BRIEF: "отвечаю коротко"},
    "en": {DEAF: "not accepting commands", BRIEF: "keeping answers short"},
}

#: Фразы, которые возвращают всё в обычное состояние.
#:
#: Список тут, а не в декораторе инструмента, потому что у него два
#: потребителя: сам инструмент `core.as_usual` берёт его как свои `phrases`, а
#: голосовой конвейер сверяется с ним **до** роутера. Иначе выйдет ловушка:
#: в режиме «не слушаю» ни одна реплика до роутера не доходит, а значит и
#: разбудить ассистента нечем — останется только ждать, пока срок истечёт.
#: Один список гарантирует, что гейт пропускает ровно то, что роутер узнаёт.
WAKE_PHRASES: tuple[str, ...] = (
    "проснись",
    "слушай",
    "слушай меня",
    "можешь слушать",
    "я вернулся",
    "отбой",
    "как обычно",
    "отвечай как обычно",
    "отвечай подробно",
    "отмени режим",
    "выйди из режима",
    "wake up",
    "listen",
    "i'm back",
    "as usual",
    "cancel the mode",
)


def minutes_word(count: int) -> str:
    """«1 минуту», «2 минуты», «5 минут» — по-русски это три разных слова.

    Живёт здесь, а не в инструментах: сроком описываются и режимы, и ответы про
    них, и произносится всё это вслух — «на 21 минут» режет слух заметнее, чем
    кажется при чтении глазами.
    """
    if 11 <= count % 100 <= 14:
        return "минут"
    tail = count % 10
    if tail == 1:
        return "минуту"
    if 2 <= tail <= 4:
        return "минуты"
    return "минут"


def _normalize(text: str) -> str:
    """Привести реплику к виду, в котором её сравнивают с фразами.

    Ровно то же, что делает `Utterance.normalized`: лишние пробелы, регистр и
    хвостовая пунктуация к делу не относятся.
    """
    return " ".join(text.split()).strip(" .,!?;:").lower()


def wakes_up(text: str) -> bool:
    """Означает ли реплика «вернись к обычной работе».

    Сравнение точное — намеренно. Это гейт, который стоит перед всем остальным
    в режиме «не слушаю»: нечёткое совпадение здесь означало бы просыпаться от
    случайного слова, а проверять по нему нечего — обращение по имени рядом с
    такой фразой уже требуется отдельно.
    """
    return _normalize(text) in WAKE_PHRASES


@dataclass(frozen=True, slots=True)
class Mode:
    """Включённый режим: имя плюс момент, когда он сам себя выключит."""

    name: str
    #: Монотонные секунды, когда режим истекает; 0 — бессрочно.
    until: float = 0.0

    def expired(self, now: float) -> bool:
        """Истёк ли срок к этому моменту."""
        return bool(self.until) and now >= self.until

    def remaining(self, now: float) -> float:
        """Сколько секунд осталось; 0 у бессрочного."""
        return max(0.0, self.until - now) if self.until else 0.0

    def title(self, language: str = "ru") -> str:
        """Название для человека и для подсказки модели."""
        titles = _TITLES.get(language) or _TITLES["ru"]
        return titles.get(self.name, self.name)


class Modes:
    """Набор включённых режимов с ленивым истечением срока.

    «Ленивым» — значит проверка происходит при чтении, а не по будильнику.
    Разница практическая: никаких фоновых задач, нечего останавливать при
    выходе и нечего синхронизировать между потоками.
    """

    def __init__(self) -> None:
        self._modes: dict[str, Mode] = {}

    def on(self, name: str, *, minutes: float = 0.0) -> Mode:
        """Включить режим, необязательно на время.

        :param minutes: срок жизни; 0 — пока не выключат.
        """
        until = time.monotonic() + minutes * 60 if minutes > 0 else 0.0
        mode = Mode(name=name, until=until)
        self._modes[name] = mode
        if minutes > 0:
            logger.info("Режим «%s» включён на %.0f мин", mode.title(), minutes)
        else:
            logger.info("Режим «%s» включён", mode.title())
        return mode

    def off(self, name: str) -> bool:
        """Выключить режим. Возвращает, был ли он включён."""
        mode = self._modes.pop(name, None)
        if mode is None:
            return False
        logger.info("Режим «%s» выключен", mode.title())
        return True

    def clear(self) -> tuple[Mode, ...]:
        """Выключить всё разом и вернуть то, что было включено.

        Это escape hatch: что бы ни накопилось, одна фраза возвращает
        ассистента к обычному поведению. Разбираться, какой именно режим мешает,
        голосом неудобно.
        """
        was = self.all()
        self._modes.clear()
        for mode in was:
            logger.info("Режим «%s» выключен", mode.title())
        return was

    def get(self, name: str) -> Mode | None:
        """Вернуть режим, если он включён и не истёк."""
        mode = self._modes.get(name)
        if mode is None:
            return None
        if mode.expired(time.monotonic()):
            del self._modes[name]
            logger.info("Режим «%s» истёк", mode.title())
            return None
        return mode

    def active(self, name: str) -> bool:
        """Включён ли режим прямо сейчас."""
        return self.get(name) is not None

    def all(self) -> tuple[Mode, ...]:
        """Все включённые режимы; истёкшие по дороге отсеиваются."""
        return tuple(
            mode for mode in list(self._modes.values()) if self.get(mode.name) is not None
        )

    def describe(self, language: str = "ru") -> str:
        """Перечислить включённые режимы одной строкой.

        Пусто, если ничего не включено: в подсказку модели такая строка не
        попадёт вовсе, а не займёт место словом «нет».
        """
        parts: list[str] = []
        now = time.monotonic()
        for mode in self.all():
            left = mode.remaining(now)
            if not left:
                parts.append(mode.title(language))
                continue
            # Округляем вверх: «ещё 0 минут» — это не срок, а недоразумение.
            minutes = max(1, -(-int(left) // 60))
            if language == "ru":
                parts.append(f"{mode.title(language)} (ещё {minutes} {minutes_word(minutes)})")
            else:
                parts.append(f"{mode.title(language)} (for {minutes} min)")
        return ", ".join(parts)
