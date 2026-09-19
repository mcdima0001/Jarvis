"""Клавиатура как вход и как выход: наблюдатель, реакции и голосовой ввод.

Три способности в одном скилле, все про клавиатуру:

* **Наблюдатель.** «Пишу "курс рубля", и Джарвис отвечает, не дожидаясь Enter».
  Вейкворд для клавиатуры: набранное копится в коротком буфере, буфер сверяется
  с закрытым списком фраз, совпадение уходит в систему как сказанная вслух
  команда (событие `CommandTyped`).
* **Реакции.** Живость: на набранные слова («не работает», «дедлайн») ассистент
  роняет ироничную реплику. Это речь, о которой не просили, поэтому она идёт
  через политику `Announcer` — та и не даёт острить чаще раза в минуту. Список
  реплик местный: наружу уходит только совпавшая фраза, не весь набор.
* **Голосовой ввод.** «Впиши …», «набери …» — ассистент печатает продиктованное
  в активное поле через SendInput. Enter не жмёт: вписать — его дело, отправить
  решает человек. Свой же ввод помечен как «вставленный» и наблюдателем
  пропускается, иначе ассистент среагировал бы на то, что напечатал сам.

Устройство и границы, которые тут важнее кода:

* **Закрытый список триггеров, а не запись всего.** Джарвис реагирует только на
  твои фразы. Остальной набор проходит сквозь короткий буфер (последние
  несколько десятков символов) и тут же забывается: на диск и в лог не пишется
  никогда. Это и есть разница между «вейквордом для клавиатуры» и настоящим
  логгером нажатий.
* **Сопоставление местное.** Триггеры ловятся сравнением строк в Python, без
  сети и без модели. Наружу уходит только сработавшая команда — тем же
  событием `CommandTyped`, что понимает голосовой конвейер, — а он проводит её
  через тот же роутер. «Новый вход не даёт новых прав» соблюдается буквально.
* **Владелец выбрал охват «вся система».** Значит, в буфер на уровне ОС попадают
  и пароли, и номера карт до маскировки. Защита от хранения — закрытый список и
  короткий буфер; защита от реакции в чужом месте — пропуск окон из
  `skip_windows` (менеджеры паролей, банки). Совсем читать их всё же можно;
  единственная твёрдая гарантия здесь — что реагируем мы лишь на свои фразы.
* **Выключено по умолчанию** (`keys.enabled`). И гасится на ходу голосом:
  «перестань следить за клавиатурой» — важный предохранитель для такой штуки.

Захват — тонкая обёртка над низкоуровневым хуком Windows и проверяется живьём,
как у скилла `windows`. Вся логика (буфер, совпадение, пропуск окна) — чистые
функции, и на них тесты.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping
from ctypes import wintypes
from pathlib import Path
from typing import Any, NamedTuple

from jarvis.core.attention import LOW, URGENT
from jarvis.core.contracts import CommandTyped, ToolResult
from jarvis.core.errors import LLMError, MemoryError_
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.state import DEAF
from jarvis.core.tools import tool

#: Ошибки модели, при которых текст впечатывается дословно, а не теряется.
LLMErrors = (LLMError, TimeoutError, OSError)

#: Как причёсывать продиктованное. Модель переписывает, а не отвечает: слышит
#: она рваную устную речь, а вернуть должна аккуратное сообщение и ничего сверх.
_POLISH = (
    "Ты — редактор. Тебе дают надиктованный вслух текст: рваный, с оговорками, "
    "без знаков препинания. Перепиши его в аккуратное, грамотное сообщение на "
    "том же языке. Сохрани смысл и все факты, не добавляй ничего от себя, не "
    "отвечай на текст и не комментируй. Верни ТОЛЬКО переписанный текст, без "
    "кавычек и пояснений."
)

#: Запрос модели на реплику впрок (когда включён `react_llm`). Просим одну
#: короткую ироничную реплику в характере Джарвиса, а не ответ по существу.
#:
#: **Реплика пишется на слово, а не на предложение**, и это не мелочь. Звучать
#: она будет в следующий раз, когда слово наберут снова, — то есть в другом
#: разговоре. Ответ на конкретную фразу к тому моменту окажется не к месту, а
#: реплика на тему подойдёт и тогда. Набранное идёт как подсказка о том, чем
#: человек занят, — не более.
_REACT_PROMPT = (
    "Ты — Джарвис из «Железного человека»: сдержанный, с достоинством, с тонкой "
    "иронией. Пользователь печатает за компьютером и набрал слово «{keyword}» "
    "(рядом было: «{context}»). Придумай ОДНУ короткую реплику, которую уместно "
    "обронить в ответ на это слово — в любой раз, когда оно всплывёт, а не "
    "только сейчас. Не больше восьми слов, можно «сэр». Только реплику, без "
    "пояснений и кавычек."
)

#: Сколько последних символов держать в буфере, если в конфиге не сказано иное.
#: Больше самой длинной фразы с запасом — и не больше: буфер не архив набранного,
#: а окно ровно чтобы поймать фразу на стыке нажатий.
DEFAULT_WINDOW = 64

#: Сколько молчать по одному и тому же триггеру после срабатывания, секунд.
#: Без паузы удержанная клавиша или повтор фразы выстрелили бы очередью.
DEFAULT_COOLDOWN = 4.0

#: Сколько реплик, сочинённых моделью, держать на одно слово. Предел нужен:
#: без него вечер работы превратил бы список в свалку, а кеш синтеза — в мусор,
#: который вытеснит служебные фразы ассистента.
LEARNED_PER_WORD = 12
#: Раздел памяти, где реплики переживают перезапуск.
REACTIONS_SECTION = "reactions"

#: Слежение выключено, пока владелец явно не включит. Гарантия в коде, а не в
#: конфиге: пустой конфиг на свежей машине не должен поднять кейлоггер молча.
DEFAULT_ENABLED = False

#: Триггеры по умолчанию: фраза → команда, которую отдать роутеру. Команда — то
#: же, что сказал бы вслух: она пойдёт через ту же цепочку резолверов.
DEFAULT_TRIGGERS: dict[str, str] = {
    "курс рубля": "курс рубля",
    "курс доллара": "курс доллара",
    "курс евро": "курс евро",
    "какая погода": "какая погода",
}

class Reaction(NamedTuple):
    """Что сработало на наборе: слово, готовая реплика и недавний контекст."""

    keyword: str
    quip: str
    context: str


#: Реакции по умолчанию: подстрока в наборе → ироничные реплики, из которых
#: выбирается по очереди. Это не команды: их ассистент говорит сам, поэтому они
#: идут через политику речи без вопроса (пауза между репликами, тихие часы) —
#: она и не даёт ему острить каждые несколько секунд. Список большой намеренно:
#: с пятью словами живость быстро приедается. Правится в конфиге под свой стиль.
DEFAULT_REACTIONS: dict[str, tuple[str, ...]] = {
    "не работает": ("Как всегда, сэр.", "Ожидаемо.", "Опять оно."),
    "не запускается": ("Классика жанра, сэр.", "Ну разумеется."),
    "почему": ("Хороший вопрос, сэр.", "Вот и я думаю."),
    "дедлайн": ("Звучит напряжённо.", "Оптимистично, сэр."),
    "устал": ("Держитесь, сэр.", "Может, перерыв?"),
    "кофе": ("Отличная идея, сэр.", "Одобряю."),
    "гений": ("Скромность украшает, сэр.",),
    "не понимаю": ("Присоединяюсь, сэр.", "Мы оба, сэр."),
    "баг": ("Это не баг, сэр.", "Фича, я полагаю."),
    "ошибка": ("Бывает у лучших, сэр.", "Экспериментально."),
    "получилось": ("Поздравляю, сэр.", "Не сомневался."),
    "готово": ("Впечатляет, сэр.",),
    "наконец": ("Терпение вознаграждается, сэр.",),
    "сдаюсь": ("Рано, сэр.", "Ещё один заход?"),
    "переделать": ("С удовольствием, сэр.", "Ну конечно."),
    "лень": ("Понимаю, сэр.", "Кто бы говорил."),
    "гениально": ("Не буду спорить, сэр.",),
    "не помню": ("Для этого есть я, сэр.",),
    "срочно": ("Как обычно, сэр.",),
    "завтра": ("Знакомое слово, сэр.",),
    "потом": ("То есть никогда, сэр?",),
    "зависло": ("Терпение, сэр.", "Дайте ему минуту."),
    "паника": ("Спокойствие, сэр.", "Только спокойствие."),
    "идеально": ("Как и всё у вас, сэр.",),
    "ненавижу": ("Сильно сказано, сэр.",),
    "работает": ("Не трогайте, сэр.", "Вот и славно."),
}

#: Окна, в которых не следим вовсе: их заголовки выдают ввод, который не должен
#: попадать даже в короткий буфер. Совпадение — по подстроке в заголовке.
DEFAULT_SKIP = (
    "bitwarden",
    "keepass",
    "1password",
    "lastpass",
    "парол",  # стеблем, чтобы ловить и «пароль», и «пароля», и «пароли»
    "password",
    "банк",
    "bank",
    "сбербанк",
    "тинькофф",
    "tinkoff",
)


#: Признаки игры в пути к программе активного окна. В игре WASD и чат —
#: не текст: 19.09.2026 в Minecraft прозвучало «раскладка не та». Minecraft
#: Java идёт как `javaw.exe` и обычно в окне, поэтому одного «во весь экран» мало.
DEFAULT_GAMES = (
    "steamapps",
    "epic games",
    "riot games",
    "battle.net",
    "ubisoft",
    "ea games",
    "xboxgames",
    "gog galaxy\\games",
    "minecraft",
    "javaw.exe",
)


def is_game(path: str, fullscreen: bool, patterns: tuple[str, ...]) -> bool:
    """Похоже ли активное окно на игру: полноэкранный Direct3D или программа из игрового места."""
    low = path.lower().replace("/", "\\")
    return fullscreen or any(mark in low for mark in patterns)


def normalize(text: str) -> str:
    """Привести к виду для сравнения: нижний регистр, одиночные пробелы."""
    return " ".join(text.split()).lower()


def ends_with_word(buffer: str, phrase: str) -> bool:
    """Кончается ли набранное фразой, начатой с начала слова.

    Без границы «доработает» будило реакцию на «работает», а «дебаг» — на
    «баг» (живой запуск 14.09.2026): совпадение искалось как окончание строки.
    """
    if not buffer.endswith(phrase):
        return False
    before = buffer[: -len(phrase)]
    return not before or not before[-1].isalnum()


#: Слова, отрицающие следующее за ними: «не очень работает» — не «работает».
_NEGATIONS = ("не", "ни", "нет", "нифига", "никак")


def negated(typed: str, pattern: str) -> bool:
    """Отрицается ли слово из списка стоящим перед ним «не» — в пределах двух слов.

    Своя реакция на «не работает» в списке есть и побеждает сама, как более
    длинная. А «не очень работает» и «не особо работает» будили «Вот и славно».
    Смотрим только в пределах своей части фразы: «не спал, но работает» — уже
    не отрицание.
    """
    head = typed[: -len(pattern)] if typed.endswith(pattern) else typed
    clause = re.split(r"[,.;:!?—\-\"«»()]", head)[-1]
    return any(word in _NEGATIONS for word in clause.split()[-2:])


def is_sensitive(title: str, patterns: tuple[str, ...]) -> bool:
    """Похоже ли активное окно на то, где следить нельзя.

    По заголовку, а не по процессу: браузерная вкладка банка и менеджер паролей
    подписаны в заголовке, а тащить ради этого перечисление процессов на каждое
    нажатие незачем. Проверка грубая и честно таковой остаётся — твёрдую
    гарантию даёт закрытый список триггеров, а не этот фильтр.
    """
    low = title.lower()
    return any(mark in low for mark in patterns)


class Triggers:
    """Скользящий буфер набранного и совпадение с закрытым списком фраз.

    Чистый, без всякой ОС: на нём и держится проверяемость. Копит символы,
    держит последние `window`, после каждого сверяет хвост с фразами и, совпав,
    отдаёт команду и очищается — чтобы то же самое не сработало второй раз с
    продолжением набора.
    """

    def __init__(
        self,
        mapping: Mapping[str, str],
        *,
        window: int = DEFAULT_WINDOW,
        cooldown_s: float = DEFAULT_COOLDOWN,
    ) -> None:
        #: Нормализованная фраза → команда. Пустые ключи выкидываем сразу.
        self._commands = {
            normalize(phrase): command
            for phrase, command in mapping.items()
            if phrase.strip()
        }
        #: Длинные фразы проверяем первыми: «курс доллара» должен побеждать
        #: «курс», иначе более общий триггер перехватит более точный.
        self._phrases = sorted(self._commands, key=len, reverse=True)
        longest = max((len(phrase) for phrase in self._phrases), default=0)
        #: Окно не короче самой длинной фразы плюс запас — иначе её не поймать.
        self._window = max(window, longest + 8)
        self._cooldown = max(0.0, cooldown_s)
        self._buffer = ""
        self._fired: dict[str, float] = {}

    @property
    def phrases(self) -> tuple[str, ...]:
        """Отслеживаемые фразы, длинные первыми."""
        return tuple(self._phrases)

    def reset(self) -> None:
        """Забыть накопленное: новая строка, чужое окно, сработавший триггер."""
        self._buffer = ""

    def backspace(self) -> None:
        """Стереть последний символ — Backspace правит и наш буфер тоже."""
        self._buffer = self._buffer[:-1]

    def feed(self, char: str, *, now: float | None = None) -> str | None:
        """Добавить символ и проверить, не сложилась ли фраза.

        :param char: один печатный символ (уже переведённый из нажатия).
        :param now: текущее время; по умолчанию — сейчас. Для тестов.
        :return: команду для роутера, если хвост буфера совпал с триггером и тот
            не на паузе; иначе ``None``.
        """
        if not char:
            return None
        self._buffer = (self._buffer + char.lower())[-self._window :]
        moment = time.monotonic() if now is None else now
        for phrase in self._phrases:
            # Только с начала слова. Конец слова команде не ждём: «курс рубля»
            # отвечает, не дожидаясь пробела, — ради этого наблюдатель и затевался.
            if not ends_with_word(self._buffer, phrase):
                continue
            last = self._fired.get(phrase)
            if last is not None and moment - last < self._cooldown:
                # На паузе: тот же триггер только что срабатывал. Буфер всё
                # равно чистим, чтобы он не «висел» совпавшим до конца паузы.
                self.reset()
                return None
            self._fired[phrase] = moment
            self.reset()
            return self._commands[phrase]
        return None

    def feed_word(self, word: str, *, now: float | None = None) -> str | None:
        """Подать слово целиком — удобный ярлык поверх `feed`.

        :return: команду, если по ходу набора сложился триггер; иначе ``None``.
        """
        result: str | None = None
        for char in word:
            got = self.feed(char, now=now)
            if got is not None:
                result = got
        return result


class Reactions:
    """То же совпадение по буферу, но ответ — ироничная реплика, не команда.

    Отличий от `Triggers` два, и оба по делу. Во-первых, на одну подстроку
    приходится несколько реплик, и они выдаются по кругу — иначе живость
    оборачивается попугаем. Во-вторых, пауза по умолчанию длиннее: острить на
    каждое «почему» невыносимо, а редко — забавно. Глобально частоту всё равно
    держит политика речи без вопроса, у которой своя пауза между репликами.
    """

    def __init__(
        self,
        mapping: Mapping[str, tuple[str, ...] | list[str]],
        *,
        window: int = DEFAULT_WINDOW,
        cooldown_s: float = 60.0,
    ) -> None:
        self._quips = {
            normalize(pattern): tuple(quips)
            for pattern, quips in mapping.items()
            if pattern.strip() and quips
        }
        self._patterns = sorted(self._quips, key=len, reverse=True)
        longest = max((len(pattern) for pattern in self._patterns), default=0)
        self._window = max(window, longest + 8)
        self._cooldown = max(0.0, cooldown_s)
        self._buffer = ""
        self._fired: dict[str, float] = {}
        #: Какую реплику выдали прошлый раз — чтобы идти по кругу, а не повторять.
        self._turn: dict[str, int] = {}
        #: Сочинённое моделью впрок, отдельно от заданного в конфиге: вытесняется
        #: только оно. Переживает перезапуск через `snapshot`/`restore`.
        self._learned: dict[str, tuple[str, ...]] = {}

    def snapshot(self) -> dict[str, dict[str, object]]:
        """Что стоит пережить перезапуск: сочинённое и место в круге каждого слова.

        Жалоба владельца 19.09.2026: «какие фразы были, такие и остались». За
        день десяток перезапусков, и каждый стирал сочинённое и сбрасывал круг —
        на «работает» трижды подряд звучало одно и то же «Не трогайте, сэр».
        """
        return {
            "learned": {pattern: list(lines) for pattern, lines in self._learned.items()},
            "turn": dict(self._turn),
        }

    def restore(self, data: Mapping[str, object]) -> None:
        """Вернуть сохранённое; слова, которых больше нет в списке, забываются."""
        learned = data.get("learned")
        if isinstance(learned, Mapping):
            for pattern, lines in learned.items():
                if pattern in self._quips and isinstance(lines, list):
                    clean = tuple(" ".join(str(line).split()) for line in lines if str(line).strip())
                    self._learned[pattern] = clean[-LEARNED_PER_WORD:]
        turn = data.get("turn")
        if isinstance(turn, Mapping):
            for pattern, index in turn.items():
                if pattern in self._quips and isinstance(index, int):
                    self._turn[pattern] = index

    @property
    def patterns(self) -> tuple[str, ...]:
        """Отслеживаемые подстроки, длинные первыми."""
        return tuple(self._patterns)

    def reset(self) -> None:
        """Забыть накопленное — новая строка или чужое окно."""
        self._buffer = ""

    def backspace(self) -> None:
        """Стереть последний символ."""
        self._buffer = self._buffer[:-1]

    def feed(self, char: str, *, now: float | None = None) -> "Reaction | None":
        """Добавить символ и, если только что закончилось слово из списка, вернуть реакцию.

        **Реакция ждёт конца слова** — пробела, знака, Enter (`finish`). Иначе
        «баг» срабатывал бы посреди «багаж», а «работает» — посреди «работаете».
        Начало слова проверяется тоже: «доработает» — не «работает».

        Возвращается не только реплика, но и совпавшее слово и недавний набор:
        для готовой реплики хватит первого, а модель, если её включили, сочинит
        по контексту. Буфер, в отличие от триггеров, **не чистится** после
        срабатывания: стирать контекст незачем — от повтора защищает пауза.
        """
        if not char:
            return None
        self._buffer = (self._buffer + char.lower())[-self._window :]
        if char.isalnum():
            return None  # слово ещё не кончилось
        return self._match(self._buffer[:-1], time.monotonic() if now is None else now)

    def finish(self, *, now: float | None = None) -> "Reaction | None":
        """Строка кончилась (Enter): последнее слово тоже закончено."""
        return self._match(self._buffer, time.monotonic() if now is None else now)

    def _match(self, typed: str, moment: float) -> "Reaction | None":
        """Реакция на слово, которым кончается `typed`, если оно в списке."""
        for pattern in self._patterns:
            if not ends_with_word(typed, pattern):
                continue
            if not pattern.startswith(_NEGATIONS) and negated(typed, pattern):
                # «не очень работает»: шутить «Вот и славно» тут невпопад.
                return None
            last = self._fired.get(pattern)
            if last is not None and moment - last < self._cooldown:
                return None
            self._fired[pattern] = moment
            quips = self.options(pattern)
            index = self._turn.get(pattern, -1) + 1
            self._turn[pattern] = index
            return Reaction(
                keyword=pattern, quip=quips[index % len(quips)], context=typed
            )
        return None

    def learn(self, keyword: str, quip: str) -> bool:
        """Добавить реплику, сочинённую моделью, в оборот этого слова.

        Смысл — в сроках. Спросить модель в момент срабатывания значит заставить
        человека ждать: реплика от неё идёт около полусекунды, а синтез свежего
        текста — ещё полторы (замер 12.09.2026). Живости от шутки, опоздавшей на
        две секунды, не прибавляется. Поэтому модель пишет **впрок**: на слово
        отвечает готовая реплика сразу, а сочинённая ложится сюда и звучит в
        следующий раз — уже мгновенно, потому что и синтез к тому времени готов.

        **Сочинённое лежит отдельно от заданного в конфиге**, и вытесняется
        только оно. Список владельца — то, чему он доверяет; затирать его
        выдумками модели нельзя, даже когда их накопилось больше.

        :return: добавили ли реплику (нет — слово чужое, пусто или такая уже есть).
        """
        pattern = normalize(keyword)
        line = " ".join(quip.split())
        if pattern not in self._quips or not line:
            return False
        if line in self._quips[pattern] or line in self._learned.get(pattern, ()):
            return False
        self._learned[pattern] = (self._learned.get(pattern, ()) + (line,))[
            -LEARNED_PER_WORD:
        ]
        return True

    def options(self, keyword: str) -> tuple[str, ...]:
        """Все реплики слова: заданные в конфиге и сочинённые моделью."""
        pattern = normalize(keyword)
        return self._quips.get(pattern, ()) + self._learned.get(pattern, ())

    def feed_pattern(self, text: str, *, now: float | None = None) -> "Reaction | None":
        """Подать подстроку целиком — ярлык поверх `feed` для тестов и удобства.

        :return: реакцию, если по ходу набора сложилась подстрока; иначе ``None``.
        """
        result: "Reaction | None" = None
        for char in text:
            got = self.feed(char, now=now)
            if got is not None:
                result = got
        # Конец подачи — как Enter: последнее слово закончено, строка начинается
        # заново. Без сброса два вызова подряд склеивались в «почемупочему».
        result = result or self.finish(now=now)
        self.reset()
        return result


# --- раскладка --------------------------------------------------------------

#: Одни и те же клавиши в двух раскладках: QWERTY и ЙЦУКЕН.
LAYOUT_LATIN = "qwertyuiop[]asdfghjkl;'zxcvbnm,.`"
LAYOUT_CYRILLIC = "йцукенгшщзхъфывапролджэячсмитьбюё"
_TO_CYRILLIC = dict(zip(LAYOUT_LATIN, LAYOUT_CYRILLIC, strict=True))
_TO_LATIN = dict(zip(LAYOUT_CYRILLIC, LAYOUT_LATIN, strict=True))
#: Те же клавиши с Shift: без них «Rfr ltkf&» исправилось бы в «Как дела&».
_SHIFTED_LATIN = 'QWERTYUIOP{}ASDFGHJKL:"ZXCVBNM<>~/?@#$^&|'
_SHIFTED_CYRILLIC = 'ЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮЁ.,"№;:?/'
_FIX_TO_CYRILLIC = dict(zip(LAYOUT_LATIN + _SHIFTED_LATIN, LAYOUT_CYRILLIC + _SHIFTED_CYRILLIC, strict=True))
_FIX_TO_LATIN = {cyrillic: latin for latin, cyrillic in _FIX_TO_CYRILLIC.items()}


def swap_layout(text: str, direction: str) -> str:
    """Тот же набор клавиш в другой раскладке: «ru» — латиницу в кириллицу, «en» — обратно.

    Пробелы, цифры и всё, что в обеих раскладках на месте, не трогаются.
    """
    table = _FIX_TO_CYRILLIC if direction == "ru" else _FIX_TO_LATIN
    return "".join(table.get(char, char) for char in text)
#: Знаки, которые в другой раскладке — буквы (ж, э, х, ъ, ё, б, ю).
_LAYOUT_MARKS = ";'[]`,."

#: Слова короче не судим: «d», «b», «rfr» — это «в», «и», «как», но и «db»,
#: «ok» тоже. Они не засчитываются и не сбивают счёт.
LAYOUT_MIN_LETTERS = 4
#: Насколько слово в другой раскладке должно быть вероятнее, чем как набрано.
#: Замер 15.09.2026 (`tools/layout_model.py`, отложенная половина слов от четырёх
#: букв): русское латиницей ловится в 96.0%, ложно на английском — 0.21%;
#: английское кириллицей — 92.1%, ложно на русском — 0.00%.
LAYOUT_MARGIN = 1.0
#: Сколько таких слов подряд, чтобы заговорить. Одно слово — не повод: имена в
#: коде и названия бывают любыми, а двух подряд ложных почти не бывает.
LAYOUT_WORDS = 2
#: Паузы по времени у замечания нет (просьба владельца 15.09.2026): на
#: «Rfr ltkf& Xnj ltkftim&» через двадцать секунд после первого замечания
#: двухминутная пауза промолчала, и выглядело это как «не понял». Повтор
#: сдерживает другое — см. `LayoutGuard`: пока пишут не той раскладкой,
#: замечание одно; снова — после слова в верной раскладке или Enter.

#: Частые короткие слова: модели пар букв на двух-трёх буквах судить не по чему,
#: а «как», «что», «ну», «да» — половина живого текста. Слово из списка,
#: набранное в другой раскладке, засчитывается целиком. Отобраны так, чтобы
#: форма в другой раскладке не встречалась в своём языке (проверено по тем же
#: корпусам, что и модель): выброшены «мы» (vs), «че» (xt), «ща» (of).
SHORT_RUSSIAN = (
    "как что это так вот уже где кто там тут ну да нет мне вы ты он она они оно его её ещё еще "
    "все всё без для про под над при или чем мой моя мои твой тоже раз два три час ага угу щас "
    "чё не ни по из за до от на со же ли бы вам нам им ей ему тем том той эта эти тот уж вон "
    "эх ой ох ах хм мда нас вас них ним нее неё был была было быть есть чей кем чём"
).split()
SHORT_ENGLISH = (
    "the and you is it to of in on at be we are for not but can yes no ok so do my me how hi "
    "hey all any was has had his her him its our out new now one two who why yet use get got "
    "set see let did too off own way may say she they them this that with from have your"
).split()
#: Форма в чужой раскладке → каким языком слово было на самом деле.
_SHORT_WRONG: dict[str, str] = {
    **{"".join(_TO_LATIN[char] for char in word): "ru" for word in SHORT_RUSSIAN},
    **{"".join(_TO_CYRILLIC[char] for char in word): "en" for word in SHORT_ENGLISH},
}
LAYOUT_MODEL = Path(__file__).with_name("layout_model.json")

#: Реплики: «ru» — русский в английской раскладке, «en» — наоборот. Набранное не
#: цитируется намеренно: в не той раскладке набирают и пароли.
DEFAULT_LAYOUT_QUIPS: dict[str, tuple[str, ...]] = {
    "ru": (
        "Смелый шифр, сэр. Но раскладка, кажется, английская.",
        "Сэр, раскладка не та.",
        "Любопытный язык, сэр. Подозрительно похож на русский.",
        "Сэр, это русский, просто в английской раскладке.",
        "Похоже на пароль от Пентагона, сэр. Или на русский латиницей.",
        "Раскладка, сэр. Опять она.",
    ),
    "en": (
        "Сэр, это английский в русской раскладке.",
        "Интересная кириллица, сэр. Раскладка не та.",
        "Раскладка русская, сэр, а слова — нет.",
        "Сэр, по-английски это читалось бы лучше.",
        "Древнерусский английский, сэр? Раскладка не та.",
        "Сэр, кириллица не оценит ваш английский.",
    ),
}


#: Добавка к замечанию, когда текст уже переписан в верной раскладке.
LAYOUT_FIXED = "Поправил."


class LayoutModel:
    """Вероятности пар букв для русского и английского — чтобы узнать не ту раскладку."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._tables = {language: data[language] for language in ("ru", "en")}

    @classmethod
    def load(cls, path: Path = LAYOUT_MODEL) -> "LayoutModel | None":
        """Прочитать модель; ``None`` — файла нет или он испорчен."""
        try:
            return cls(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            return None

    def score(self, language: str, word: str) -> float:
        """Средний логарифм вероятности пар букв слова — чем выше, тем правдоподобнее."""
        table = self._tables[language]
        pairs, unseen, floor = table["pairs"], table["unseen"], table["floor"]
        padded = f"^{word}$"
        total = 0.0
        for first, second in zip(padded, padded[1:], strict=False):
            value = pairs.get(first + second)
            total += value if value is not None else unseen.get(first, floor)
        return total / (len(padded) - 1)

    def wrong_layout(self, word: str) -> str | None:
        """«ru» — русское слово в английской раскладке, «en» — наоборот, ``None`` — всё верно."""
        word = word.lower()
        if sum(char.isalpha() for char in word) < LAYOUT_MIN_LETTERS:
            return None
        if all(char in _TO_CYRILLIC for char in word):
            swapped = "".join(_TO_CYRILLIC[char] for char in word)
            return "ru" if self.score("ru", swapped) - self.score("en", word) > LAYOUT_MARGIN else None
        if all(char in _TO_LATIN for char in word):
            swapped = "".join(_TO_LATIN[char] for char in word)
            return "en" if self.score("en", swapped) - self.score("ru", word) > LAYOUT_MARGIN else None
        return None


class LayoutGuard:
    """Копит набранные слова и замечает, когда несколько подряд — не в той раскладке.

    Слово кончается на пробеле, знаке или Enter. Короткие слова вне списка частых
    не считаются и счёт не сбивают; слово в верной раскладке — сбивает.
    Сработав, молчит, пока человек не переключится: слово в верной раскладке или
    Enter (новое сообщение) снова взводят сторожа. Паузы по времени нет.

    Заодно помнит **строку** — что набрано в поле с последнего места, где курсор
    мог сдвинуться (`break_line`), — и, сработав, откуда в ней начался текст не в
    той раскладке. Это `pending_fix`: наблюдатель стирает ровно столько и
    впечатывает то же самое в верной раскладке (просьба владельца 17.09.2026).
    Строка живёт только в памяти и никуда не пишется.
    """

    def __init__(self, model: LayoutModel, *, words: int = LAYOUT_WORDS) -> None:
        self._model = model
        self._need = max(1, words)
        self._word = ""
        self._streak = 0
        self._direction = ""
        #: Можно ли сейчас заметить. Снимается срабатыванием, взводится переключением.
        self._armed = True
        #: Набранное в поле с места, где курсор последний раз мог сдвинуться.
        self._line = ""
        #: Где в строке начинается слово, которое сейчас набирают.
        self._word_start = 0
        #: Докуда строка набрана в верной раскладке: дальше — кандидат в правку.
        self._clean_end = 0
        #: Откуда править, если сработали и правка ещё не сделана.
        self._fix_start: int | None = None
        self._fix_direction = ""

    def reset(self) -> None:
        """Забыть недописанное слово и счёт — сработала команда или чужое окно."""
        self._streak = 0
        self.break_line()

    def break_line(self) -> None:
        """Курсор мог уйти (стрелки, сочетание, другое окно): набранное раньше не править."""
        self._line = ""
        self._word = ""
        self._word_start = self._clean_end = 0
        self._fix_start = None

    def backspace(self) -> None:
        """Стереть последний символ слова."""
        self._word = self._word[:-1]
        self._line = self._line[:-1]
        self._clean_end = min(self._clean_end, len(self._line))
        if self._fix_start is not None and self._fix_start > len(self._line):
            self._fix_start = None

    def feed(self, char: str, *, now: float | None = None) -> str | None:
        """Добавить символ; на конце слова, если набралось нужное число подряд, — направление."""
        self._line += char
        lowered = char.lower()
        if lowered.isalpha() or lowered in _LAYOUT_MARKS:
            if not self._word:
                self._word_start = len(self._line) - 1
            self._word += lowered
            return None
        return self._close(now)

    def finish(self, *, now: float | None = None) -> str | None:
        """Строка кончилась (Enter): закрыть последнее слово и начать счёт заново.

        Править после Enter нечего: сообщение, скорее всего, уже ушло.
        """
        result = self._close(now)
        self._streak = 0
        self._armed = True  # новое сообщение — снова можно заметить
        self.break_line()
        return result

    def pending_fix(self) -> tuple[str, str] | None:
        """Что исправить после срабатывания: направление и набранное не той раскладкой."""
        if self._fix_start is None:
            return None
        typed = self._line[self._fix_start:]
        return (self._fix_direction, typed) if typed.strip() else None

    def fixed(self, replacement: str) -> None:
        """Правка впечатана: в строке теперь верный текст."""
        if self._fix_start is None:
            return
        self._line = self._line[: self._fix_start] + replacement
        self._clean_end = len(self._line)
        self._word = ""
        self._fix_start = None

    def cancel_fix(self) -> None:
        """Править не вышло или нельзя — забыть о правке."""
        self._fix_start = None

    def _close(self, now: float | None) -> str | None:
        # «,» и «.» в конце — обычные знаки, а не «б» и «ю».
        word = self._word.rstrip(",.")
        self._word = ""
        direction = _SHORT_WRONG.get(word)
        if direction is None:
            # Не из списка частых: короткое не судим, длинное — моделью.
            if sum(char.isalpha() for char in word) < LAYOUT_MIN_LETTERS:
                return None
            direction = self._model.wrong_layout(word)
        if direction is None:
            # Слово в верной раскладке: переключился — следующая ошибка снова заметна.
            self._streak = 0
            self._armed = True
            self._clean_end = len(self._line)
            return None
        if direction != self._direction:
            if self._streak:
                # Прежние слова были не той раскладкой в другую сторону — их не трогаем.
                self._clean_end = self._word_start
            self._direction, self._streak = direction, 0
        self._streak += 1
        if self._streak < self._need:
            return None
        self._streak = 0
        if not self._armed:
            return None
        self._armed = False
        self._fix_start, self._fix_direction = self._clean_end, direction
        return direction


#: Слова, которыми диктующий просит вписать буквально, без переписывания.
_VERBATIM_MARKERS = ("дословно", "буквально", "как есть", "verbatim")


def strip_verbatim_marker(text: str) -> tuple[bool, str]:
    """Отделить пометку «дословно» в начале продиктованного.

    «Впиши дословно …» — просьба не причёсывать, а набрать как сказано. Метку
    убираем, остальное возвращаем как есть.

    :return: пара «просили дословно» и текст без метки.
    """
    stripped = text.lstrip(" ,.")
    low = stripped.lower()
    for marker in _VERBATIM_MARKERS:
        if low.startswith(marker):
            rest = stripped[len(marker) :].lstrip(" ,.:—-")
            if rest:
                return True, rest
    return False, text


def unicode_events(text: str) -> list[tuple[int, bool]]:
    """Разложить текст на события клавиатуры для ввода в поле.

    Печатаем через юникод-события (KEYEVENTF_UNICODE): так символ попадает в
    поле как есть, независимо от раскладки — кириллице это необходимо. Каждый
    символ — код-юнит(ы) UTF-16, и на каждый два события: нажать и отпустить.
    Символы вне BMP (эмодзи) занимают два код-юнита — суррогатную пару, — и оба
    должны уйти, иначе вставится половина.

    :return: список пар ``(код-юнит, отпускание ли)``.
    """
    events: list[tuple[int, bool]] = []
    for char in text:
        blob = char.encode("utf-16-le")
        for i in range(0, len(blob), 2):
            unit = int.from_bytes(blob[i : i + 2], "little")
            events.append((unit, False))
            events.append((unit, True))
    return events


# --- Захват: только Windows, проверяется живьём -----------------------------

#: Низкоуровневый хук клавиатуры и коды сообщений — из WinUser.h.
_WH_KEYBOARD_LL = 13
_WM_KEYDOWN = 0x0100
_WM_SYSKEYDOWN = 0x0104
_WM_QUIT = 0x0012
_VK_BACK = 0x08
_VK_RETURN = 0x0D
_VK_SHIFT = 0x10
_VK_CAPITAL = 0x14
#: Ctrl, Alt и Win: пока зажаты, буквы — это сочетания, и строку править нельзя.
_VK_COMBO = frozenset({0x11, 0xA2, 0xA3, 0x12, 0xA4, 0xA5, 0x5B, 0x5C})
#: Своё сообщение потоку хука: сделать правку раскладки между событиями клавиатуры.
_WM_FIX_LAYOUT = 0x8000 + 1
#: Событие клавиатуры вставлено программой, а не человеком. Свой же ввод (диктовка
#: ниже) приходит с этим флагом — и его надо пропускать, иначе ассистент
#: среагирует на то, что напечатал сам.
_LLKHF_INJECTED = 0x10
#: Ввод текста в поле: юникод-символ, нажать и отпустить.
_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
#: Как часто перепроверять, не сменилось ли активное окно, секунд. На каждое
#: нажатие спрашивать ОС расточительно, а полсекунды хватает.
_FOREGROUND_TTL = 0.5


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    """Что ОС кладёт по указателю lParam при событии клавиатуры (WinUser.h)."""

    _fields_ = (
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _KEYBDINPUT(ctypes.Structure):
    """Одно событие клавиатуры для SendInput."""

    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _MOUSEINPUT(ctypes.Structure):
    """Не используется, но задаёт размер объединения INPUT — оно по мыши."""

    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT))


class _INPUT(ctypes.Structure):
    """INPUT для SendInput. Размер объединения обязан быть от большего члена —
    иначе на 64 бит структура короче, чем ждёт ОС, и ввод молча не проходит."""

    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


def type_text_os(text: str) -> int:
    """Впечатать текст в активное поле через SendInput. Возвращает число событий.

    Отдельная от хука функция и свой дескриптор user32: диктовать можно и при
    выключенном наблюдателе. Enter не жмём намеренно — человек сам решит,
    отправлять ли; наша задача только вписать.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.restype = wintypes.UINT
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]

    events = unicode_events(text)
    if not events:
        return 0
    array = (_INPUT * len(events))()
    for slot, (unit, is_up) in zip(array, events, strict=True):
        slot.type = _INPUT_KEYBOARD
        slot.u.ki = _KEYBDINPUT(
            wVk=0,
            wScan=unit,
            dwFlags=_KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if is_up else 0),
            time=0,
            dwExtraInfo=None,
        )
    sent = user32.SendInput(len(array), array, ctypes.sizeof(_INPUT))
    return int(sent)


def _process_path(hwnd: Any) -> str:
    """Путь к программе окна; не узнали — пусто."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_wchar_p, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(process)


def _d3d_fullscreen() -> bool:
    """Идёт полноэкранная Direct3D-игра — сигнал самой Windows.

    Не «окно во весь экран»: так выглядит и браузер на F11, в котором печатают
    (замечание владельца 19.09.2026). Игры в окне без рамки ловятся по пути
    к программе (`games`).
    """
    try:
        shell32 = ctypes.WinDLL("shell32")
        state = ctypes.c_int()
        # QUNS_RUNNING_D3D_FULL_SCREEN = 3
        return shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0 and state.value == 3
    except (OSError, AttributeError):
        return False


def replace_typed_os(erase: int, text: str) -> bool:
    """Стереть `erase` символов перед курсором и впечатать `text` — одним вызовом.

    Один `SendInput` не перемешивается с тем, что человек продолжает набирать:
    ОС вставляет пачку в поток ввода целиком.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.restype = wintypes.UINT
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
    keys: list[tuple[int, int, int]] = []  # (vk, scan, flags)
    for _ in range(erase):
        keys += [(_VK_BACK, 0, 0), (_VK_BACK, 0, _KEYEVENTF_KEYUP)]
    for unit, is_up in unicode_events(text):
        keys.append((0, unit, _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if is_up else 0)))
    if not keys:
        return True
    array = (_INPUT * len(keys))()
    for slot, (vk, scan, flags) in zip(array, keys, strict=True):
        slot.type = _INPUT_KEYBOARD
        slot.u.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
    return int(user32.SendInput(len(array), array, ctypes.sizeof(_INPUT))) == len(keys)


#: Язык раскладки, в которую переключать: младшее слово HKL.
_LAYOUT_LANGUAGES = {"ru": 0x0419, "en": 0x0409}
_WM_INPUTLANGCHANGEREQUEST = 0x0050


def switch_layout_os(hwnd: Any, direction: str) -> bool:
    """Попросить окно переключиться на русскую («ru») или английскую («en») раскладку."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetKeyboardLayoutList.restype = ctypes.c_int
    user32.GetKeyboardLayoutList.argtypes = [ctypes.c_int, ctypes.c_void_p]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    count = user32.GetKeyboardLayoutList(0, None)
    if count <= 0:
        return False
    layouts = (ctypes.c_void_p * count)()
    user32.GetKeyboardLayoutList(count, layouts)
    wanted = _LAYOUT_LANGUAGES[direction]
    target = next((value for value in layouts if value and value & 0xFFFF == wanted), None)
    if target is None:
        return False
    # LPARAM знаковый, а старшие биты HKL бывают выставлены: переводим без проверки.
    return bool(user32.PostMessageW(hwnd, _WM_INPUTLANGCHANGEREQUEST, 0, ctypes.c_ssize_t(target).value))


def _configure(user32: Any, kernel32: Any) -> None:
    """Объявить типы функций WinAPI.

    Без этого ctypes считает, что всё возвращает 32-битный ``int``, и на
    64-битном Python **обрезает указатели**: дескриптор раскладки, окна и хука
    превращаются в мусор, а хук просто не встаёт. Ошибка тихая, поэтому типы
    проставляем явно. Дескрипторы держим как ``c_void_p``: не все из них есть в
    ``wintypes`` под всеми версиями, а размер у всех один.
    """
    handle = ctypes.c_void_p
    user32.SetWindowsHookExW.restype = handle
    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, ctypes.c_void_p, handle, wintypes.DWORD,
    ]
    user32.CallNextHookEx.restype = wintypes.LPARAM
    user32.CallNextHookEx.argtypes = [
        handle, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.UnhookWindowsHookEx.argtypes = [handle]
    user32.GetForegroundWindow.restype = handle
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [handle, ctypes.c_void_p]
    user32.GetKeyboardLayout.restype = handle
    user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
    user32.MapVirtualKeyExW.restype = wintypes.UINT
    user32.MapVirtualKeyExW.argtypes = [wintypes.UINT, wintypes.UINT, handle]
    user32.ToUnicodeEx.restype = ctypes.c_int
    user32.ToUnicodeEx.argtypes = [
        wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
        ctypes.c_wchar_p, ctypes.c_int, wintypes.UINT, handle,
    ]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [handle]
    user32.GetWindowTextW.argtypes = [handle, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetMessageW.argtypes = [
        ctypes.c_void_p, handle, wintypes.UINT, wintypes.UINT,
    ]
    user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    kernel32.GetModuleHandleW.restype = handle
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD


class KeyboardWatcher:
    """Глобальный хук клавиатуры: переводит нажатия в символы и ловит фразы.

    Живёт на отдельном потоке ОС со своим циклом сообщений — иначе
    низкоуровневый хук не работает. При совпадении зовёт `on_command` (уже из
    потока asyncio, через переданный `to_loop`), а не лезет в шину сам.
    """

    def __init__(
        self,
        triggers: Triggers,
        *,
        on_command: Callable[[str], None],
        to_loop: Callable[[Callable[[], None]], None],
        reactions: Reactions | None = None,
        on_react: Callable[[Reaction], None] | None = None,
        skip: tuple[str, ...] = DEFAULT_SKIP,
        layout: LayoutGuard | None = None,
        on_layout: Callable[[str, bool], None] | None = None,
        fix_layout: bool = False,
        games: tuple[str, ...] | None = DEFAULT_GAMES,
    ) -> None:
        self._triggers = triggers
        self._on_command = on_command
        self._to_loop = to_loop
        self._reactions = reactions
        self._on_react = on_react
        self._layout = layout
        self._on_layout = on_layout
        self._fix_layout_enabled = fix_layout
        #: Признаки игры; ``None`` — не выключаться в играх.
        self._games = games
        #: Окно, в котором набрана строка сторожа: сменилось — строку не правим.
        self._line_window: Any = None
        self._combo: set[int] = set()
        self._skip = skip
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._hook: Any = None  # HHOOK, живёт в ОС
        self._callback: Any = None  # ссылку держим сами, иначе соберёт GC
        self._user32: Any = None
        #: Состояние клавиатуры для ToUnicodeEx: 256 байт, мы сами ведём Shift и
        #: Caps — на потоке хука GetKeyboardState им верить нельзя.
        self._keystate: Any = None
        self._sensitive_at = 0.0
        self._sensitive = False

    @property
    def running(self) -> bool:
        """Установлен ли хук прямо сейчас."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Поднять поток с хуком. Повторный вызов ничего не делает."""
        if self.running:
            return
        self._thread = threading.Thread(
            target=self._run, name="keys-hook", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Снять хук и остановить поток сообщений."""
        if self._thread_id and self._user32 is not None:
            self._user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None
        self._thread_id = 0

    # --- поток хука --------------------------------------------------------

    def _run(self) -> None:
        """Тело потока: поставить хук и крутить цикл сообщений до WM_QUIT."""
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure(user32, kernel32)
        self._user32 = user32
        self._keystate = (ctypes.c_ubyte * 256)()
        self._thread_id = kernel32.GetCurrentThreadId()

        proc_type = ctypes.CFUNCTYPE(
            ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        )

        def callback(code: int, wparam: int, lparam: int) -> int:
            if code == 0:  # HC_ACTION: событие можно читать
                try:
                    self._on_event(wparam, lparam)
                except Exception:  # noqa: BLE001 — хук не имеет права падать
                    pass
            return user32.CallNextHookEx(self._hook, code, wparam, lparam)

        # Ссылку держим на себе: соберёт сборщик — ОС позовёт мёртвый код.
        self._callback = proc_type(callback)
        self._hook = user32.SetWindowsHookExW(
            _WH_KEYBOARD_LL, self._callback, kernel32.GetModuleHandleW(None), 0
        )
        if not self._hook:
            self._thread_id = 0
            return

        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            # Хук работает в колбэке. Правку раскладки делаем здесь, между
            # событиями: колбэк обязан вернуться быстро, слать из него ввод нельзя.
            if message.message == _WM_FIX_LAYOUT:
                try:
                    self._fix_layout()
                except Exception:  # noqa: BLE001 — поток хука не имеет права падать
                    if self._layout is not None:
                        self._layout.cancel_fix()
        user32.UnhookWindowsHookEx(self._hook)
        self._hook = None

    def _on_event(self, wparam: int, lparam: int) -> None:
        """Разобрать одно событие клавиатуры."""
        info = ctypes.cast(
            ctypes.c_void_p(lparam), ctypes.POINTER(_KBDLLHOOKSTRUCT)
        ).contents
        if info.flags & _LLKHF_INJECTED:
            # Свой же ввод (диктовка) или чужая программа — не человек. Иначе
            # ассистент среагировал бы на то, что напечатал сам.
            return
        vk = int(info.vkCode)
        down = wparam in (_WM_KEYDOWN, _WM_SYSKEYDOWN)
        if vk in _VK_COMBO:
            if down:
                self._combo.add(vk)
                if self._layout is not None:
                    self._layout.break_line()
            else:
                self._combo.discard(vk)
            return

        # Модификаторы ведём сами, и на нажатие, и на отпускание.
        if vk in (_VK_SHIFT, 0xA0, 0xA1):
            self._keystate[_VK_SHIFT] = 0x80 if down else 0x00
            return
        if vk == _VK_CAPITAL and down:
            self._keystate[_VK_CAPITAL] ^= 0x01
            return
        if not down:
            return

        if self._foreground_sensitive():
            # Запретное окно или игра: не копим вовсе, и начатое забываем.
            self._forget()
            return

        if vk == _VK_BACK:
            self._triggers.backspace()
            if self._reactions is not None:
                self._reactions.backspace()
            if self._layout is not None:
                self._layout.backspace()
            return
        if vk == _VK_RETURN:
            # Enter завершает строку. До него мы и реагируем — в этом вся суть,
            # — а после него начинаем с чистого листа. Реакция ждёт конца слова,
            # и последнее слово строки заканчивает как раз Enter.
            self._triggers.reset()
            if self._layout is not None:
                direction = self._layout.finish()
                on_layout = self._on_layout
                if direction is not None and on_layout is not None:
                    # После Enter не правим: сообщение, скорее всего, уже ушло.
                    self._to_loop(lambda: on_layout(direction, False))
                    if self._reactions is not None:
                        self._reactions.reset()
                    return
            if self._reactions is not None:
                ending = self._reactions.finish()
                self._reactions.reset()
                if ending is not None and self._on_react is not None:
                    self._to_loop(lambda: self._on_react(ending))
            return

        char = self._translate(vk)
        if self._layout is not None:
            window = self._user32.GetForegroundWindow()
            # Стрелки, Delete, Tab, другое окно, сочетание: курсор мог сдвинуться.
            if not char or not char.isprintable() or self._combo or window != self._line_window:
                self._layout.break_line()
                self._line_window = window
        if not char:
            return
        command = self._triggers.feed(char)
        if command is not None:
            # Команда важнее шутки: одно нажатие не делает и то, и другое.
            self._to_loop(lambda: self._on_command(command))
            if self._reactions is not None:
                self._reactions.reset()
            if self._layout is not None:
                self._layout.reset()
            return
        reaction = self._reactions.feed(char) if self._reactions is not None else None
        on_layout = self._on_layout
        if self._layout is not None and on_layout is not None and char.isprintable() and not self._combo:
            direction = self._layout.feed(char)
            if direction is not None:
                # Не та раскладка важнее шутки: шутить над абракадаброй невпопад.
                if self._fix_layout_enabled and self._layout.pending_fix() is not None:
                    # Замечание прозвучит после правки: «поправил» говорят, поправив.
                    self._user32.PostThreadMessageW(self._thread_id, _WM_FIX_LAYOUT, 0, 0)
                else:
                    self._layout.cancel_fix()
                    self._to_loop(lambda: on_layout(direction, False))
                return
        if reaction is not None and self._on_react is not None:
            self._to_loop(lambda: self._on_react(reaction))

    def _fix_layout(self) -> None:
        """Стереть набранное не той раскладкой, впечатать то же в верной и переключить окно.

        Зовётся из цикла сообщений потока хука, поэтому строка сторожа уже
        включает всё, что успели набрать после срабатывания. Окно сменилось или
        стало запретным — не правим: стирать пришлось бы вслепую.
        """
        guard, on_layout = self._layout, self._on_layout
        if guard is None or on_layout is None:
            return
        pending = guard.pending_fix()
        if pending is None:
            return
        direction, typed = pending
        window = self._user32.GetForegroundWindow()
        if window != self._line_window or self._foreground_sensitive():
            guard.cancel_fix()
            self._to_loop(lambda: on_layout(direction, False))
            return
        replacement = swap_layout(typed, direction)
        done = replace_typed_os(len(typed), replacement)
        if done:
            guard.fixed(replacement)
            switch_layout_os(window, direction)
        else:
            guard.cancel_fix()
        self._to_loop(lambda: on_layout(direction, done))

    def _translate(self, vk: int) -> str:
        """Перевести виртуальную клавишу в символ с учётом раскладки и Shift."""
        user32 = self._user32
        hwnd = user32.GetForegroundWindow()
        thread_id = user32.GetWindowThreadProcessId(hwnd, None)
        layout = user32.GetKeyboardLayout(thread_id)
        scan = user32.MapVirtualKeyExW(vk, 0, layout)  # MAPVK_VK_TO_VSC
        buffer = (ctypes.c_wchar * 8)()
        count = user32.ToUnicodeEx(
            vk, scan, self._keystate, buffer, len(buffer), 0, layout
        )
        if count == 1:
            return str(buffer[0])
        return ""

    def _foreground_sensitive(self) -> bool:
        """Активное окно запретное или игра? Ответ кешируется на полсекунды."""
        now = time.monotonic()
        if now - self._sensitive_at < _FOREGROUND_TTL:
            return self._sensitive
        user32 = self._user32
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value
        self._sensitive = is_sensitive(title, self._skip) or (
            self._games is not None and is_game(_process_path(hwnd), _d3d_fullscreen(), self._games)
        )
        self._sensitive_at = now
        return self._sensitive

    def _forget(self) -> None:
        """Сбросить всё накопленное: в запретном окне или игре набор не наш."""
        self._triggers.reset()
        if self._reactions is not None:
            self._reactions.reset()
        if self._layout is not None:
            self._layout.reset()


class KeysSkill(Skill):
    """Следит за набранным и отдаёт совпавшие фразы роутеру как команды."""

    meta = SkillMeta(
        name="keys",
        description="Ловит набранные ключевые фразы и отвечает, не дожидаясь Enter.",
        version="0.4.1",
        platforms=("windows",),
        spoken=("клавиатура", "keyboard"),
    )

    def __init__(self) -> None:
        super().__init__()
        self._triggers: Triggers | None = None
        self._reactions: Reactions | None = None
        self._watcher: KeyboardWatcher | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._enabled = False

    async def on_setup(self) -> None:
        """Собрать триггеры, реакции и наблюдателя из настроек."""
        mapping = self.context.setting("triggers", DEFAULT_TRIGGERS) or {}
        window = int(self.context.setting("window", DEFAULT_WINDOW))
        cooldown = float(self.context.setting("cooldown_seconds", DEFAULT_COOLDOWN))
        skip = tuple(self.context.setting("skip_windows", list(DEFAULT_SKIP)))
        self._enabled = bool(self.context.setting("enabled", DEFAULT_ENABLED))
        react = bool(self.context.setting("react", True))
        quips = self.context.setting("reactions", DEFAULT_REACTIONS) or {}
        #: Переписывать ли продиктованное моделью (иначе — буквально).
        self._rewrite = bool(self.context.setting("rewrite", True))
        #: Сочинять ли реакции моделью. Дороже и отправляет недавний набор в
        #: облако при срабатывании ключа, поэтому по умолчанию выключено.
        self._react_llm = bool(self.context.setting("react_llm", False))

        self._triggers = Triggers(mapping, window=window, cooldown_s=cooldown)
        self._reactions = Reactions(quips, window=window) if react and quips else None
        self._persist_reactions = True
        #: Замечать ли не ту раскладку («ghbdtn» вместо «привет»).
        self._layout: LayoutGuard | None = None
        custom = dict(self.context.setting("layout_quips", {}) or {})
        self._layout_quips = {
            direction: tuple(custom.get(direction) or quips_default)
            for direction, quips_default in DEFAULT_LAYOUT_QUIPS.items()
        }
        self._layout_turn = 0
        if bool(self.context.setting("layout", True)):
            model = LayoutModel.load()
            if model is None:
                self.log.warning("Модель раскладки %s не прочиталась — о раскладке молчу", LAYOUT_MODEL.name)
            else:
                self._layout = LayoutGuard(model)
        #: Исправлять ли набранное не той раскладкой и переключать ли раскладку.
        self._layout_fix = bool(self.context.setting("layout_fix", True))
        if sys.platform == "win32":
            self._watcher = KeyboardWatcher(
                self._triggers,
                on_command=self._dispatch,
                to_loop=self._from_thread,
                reactions=self._reactions,
                on_react=self._react,
                skip=skip,
                layout=self._layout,
                on_layout=self._on_layout,
                fix_layout=self._layout_fix,
                games=tuple(self.context.setting("games", list(DEFAULT_GAMES)))
                if bool(self.context.setting("skip_games", True))
                else None,
            )
        self.log.info(
            "Клавиатурный наблюдатель: %s, триггеров %d, реакций %d, раскладка: %s",
            "включён" if self._enabled else "выключен",
            len(self._triggers.phrases),
            len(self._reactions.patterns) if self._reactions else 0,
            "слежу" if self._layout else "нет",
        )

    async def on_start(self) -> None:
        """Поднять хук, если он включён и мы на Windows."""
        self._loop = asyncio.get_running_loop()
        await self._load_reactions()
        if self._enabled and self._watcher is not None:
            self._watcher.start()
            self.log.info("Слежу за клавиатурой: %s", ", ".join(self._triggers.phrases))

    async def on_stop(self) -> None:
        """Снять хук."""
        if self._watcher is not None and self._watcher.running:
            await asyncio.to_thread(self._watcher.stop)

    # --- мост между потоком хука и циклом ----------------------------------

    def _from_thread(self, action: Callable[[], None]) -> None:
        """Перебросить действие с потока хука в цикл asyncio."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(action)

    def _dispatch(self, command: str) -> None:
        """Отдать совпавшую фразу роутеру — уже из потока цикла.

        В режиме «не слушаю» молчим: просили не отвлекать, и набранное так же
        не повод заговорить, как и услышанное.
        """
        if self.modes.active(DEAF):
            self.log.debug("Набрано %r, но сейчас не слушаю", command)
            return
        self.log.info("Сработал триггер: %r", command)
        self.events.emit(CommandTyped(source="keyboard", text=command))

    def _react(self, reaction: Reaction) -> None:
        """Ироничная реплика на набранное — через политику речи без вопроса.

        Это не команда: ассистента об этом не просили. Поэтому реплика идёт в
        `Announcer` важностью `LOW` и с `hold=False` — уместна только сейчас,
        держать её на потом смысла нет. Пауза между репликами и тихие часы —
        забота политики; здесь мы лишь предлагаем.

        **Реплика всегда готовая, даже когда включён `react_llm`.** Раньше при
        включённой модели её спрашивали прямо тут, и живость оборачивалась своей
        противоположностью: полсекунды на ответ модели плюс полторы на синтез
        свежего текста — шутка приходила через две секунды после слова, на
        которое шутила. Теперь модель работает **впрок**: сейчас звучит готовое
        и потому мгновенное, а сочинённое ложится в оборот к следующему разу.
        """
        if self.modes.active(DEAF):
            return
        # Что именно сработало — иначе в логе видна одна реплика («Вот и
        # славно»), и не понять, на какое слово она была (просьба 14.09.2026).
        # Хвост набора короткий: это буфер реакций, а не переписка.
        self.log.info("Реакция на «%s» (набрано: …%s)", reaction.keyword, reaction.context[-40:])
        # Повод — только сработавшее слово, не весь набор: в строке «Вы» на
        # панели видно, на что была шутка (просьба владельца 15.09.2026).
        cause = f"{reaction.keyword[:1].upper()}{reaction.keyword[1:]} [ввод с клавиатуры]"
        self.context.announcer.offer(reaction.quip, importance=LOW, hold=False, cause=cause)
        self.context.scope.spawn(self._save_reactions(), name="keys-save")
        if self._react_llm and self.context.llm.available:
            self.context.scope.spawn(
                self._write_ahead(reaction), name="keys-react"
            )

    def _on_layout(self, direction: str, fixed: bool = False) -> None:
        """Набирают не в той раскладке — сказать об этом, с иронией.

        :param fixed: набранное уже переписано в верной раскладке и раскладка
            переключена — к замечанию добавляется «поправил».

        Через политику речи без вопроса, как шутки: уместно только сейчас, частоту
        держит `Announcer`. Набранное **не цитируется** ни вслух, ни в логе: в не
        той раскладке набирают и пароли, а цитата прозвучала бы на всю комнату.
        """
        if self.modes.active(DEAF):
            return
        quips = self._layout_quips.get(direction) or DEFAULT_LAYOUT_QUIPS["ru"]
        quip = quips[self._layout_turn % len(quips)]
        self._layout_turn += 1
        if fixed:
            quip = f"{quip} {LAYOUT_FIXED}"
        self.log.info(
            "Раскладка не та: %s%s",
            "русский латиницей" if direction == "ru" else "английский кириллицей",
            ", текст исправлен" if fixed else "",
        )
        # Срочное: общая пауза между репликами для шуток, а это исключение —
        # замечание нужно сейчас, пока человек пишет (просьба владельца
        # 15.09.2026). Режим «не слушаю» проверен выше. Повод без текста:
        # набранное в не той раскладке могло быть паролем.
        self.context.announcer.offer(quip, importance=URGENT, hold=False, cause="[набор в не той раскладке]")

    async def _load_reactions(self) -> None:
        """Поднять с диска сочинённые реплики и место в круге."""
        if self._reactions is None:
            return
        try:
            data = await self.context.memory.documents.read(REACTIONS_SECTION)
        except MemoryError_ as exc:
            self._persist_reactions = False
            self.log.warning(
                "Реплики не переживут перезапуск: %s. Добавь «%s» в memory.documents", exc, REACTIONS_SECTION
            )
            return
        self._reactions.restore(data)

    async def _save_reactions(self) -> None:
        """Записать сочинённое и круг: тихо пропускаем, если раздела нет."""
        if self._reactions is None or not self._persist_reactions:
            return
        try:
            await self.context.memory.documents.update(REACTIONS_SECTION, self._reactions.snapshot())
        except MemoryError_ as exc:
            self.log.debug("Реплики не сохранились: %s", exc)

    async def _write_ahead(self, reaction: Reaction) -> None:
        """Сочинить моделью реплику на это слово и приготовить её к следующему разу.

        Две заготовки, и обе про время. Первая — сама реплика: модель отвечает
        около полусекунды, и ждать её на глазах у человека незачем. Вторая —
        синтез: свежий текст стоит ещё полторы секунды, а приготовленный звучит
        за миллисекунды, потому что лежит в кеше готовым.

        Всё это фоновая работа, поэтому и сбой тут ничего не стоит: не вышло —
        останется список из конфига, который и так работает.
        """
        prompt = _REACT_PROMPT.format(
            keyword=reaction.keyword, context=reaction.context.strip()
        )
        try:
            line = await self.context.llm.ask(prompt, task="dialog")
        except LLMErrors as error:
            self.log.debug("Модель реплику не сочинила (%s) — остаётся готовый список", error)
            return
        line = line.strip().strip("«»\"'").strip()
        if self._reactions is None or not self._reactions.learn(reaction.keyword, line):
            return
        self.log.debug("Реплика впрок на «%s»: %r", reaction.keyword, line)
        await self._save_reactions()
        # Синтез заранее: к следующему разу реплика прозвучит мгновенно.
        await self.context.tts.prewarm(line, language="ru")

    # --- голосовые переключатели ------------------------------------------

    @tool(
        name="watch",
        phrases=[
            "следи за клавиатурой",
            "следи за тем что я печатаю",
            "следи что я набираю",
            "watch the keyboard",
        ],
        reversible=False,
    )
    async def watch(self) -> str:
        """Включить слежение за набранным на клавиатуре."""
        if self._watcher is None:
            return "Следить за клавиатурой я умею только на Windows."
        self._enabled = True
        if not self._watcher.running:
            self._watcher.start()
        return "Слежу за клавиатурой, сэр."

    @tool(
        name="unwatch",
        phrases=[
            "перестань следить за клавиатурой",
            "не следи за клавиатурой",
            "хватит следить за тем что я печатаю",
            "stop watching the keyboard",
        ],
        reversible=True,
    )
    async def unwatch(self) -> str:
        """Выключить слежение за клавиатурой."""
        self._enabled = False
        if self._watcher is not None and self._watcher.running:
            await asyncio.to_thread(self._watcher.stop)
        return "Больше не слежу за клавиатурой."

    @tool(name="status", routable=False, reversible=True)
    async def status(self) -> dict:
        """Сообщить, следит ли ассистент за клавиатурой и за какими фразами."""
        watching = self._watcher is not None and self._watcher.running
        phrases = list(self._triggers.phrases) if self._triggers else []
        return {"watching": watching, "enabled": self._enabled, "triggers": phrases}

    # --- ввод текста голосом ----------------------------------------------

    @tool(routable=False, reversible=False)
    async def type_text(self, text: str = "") -> ToolResult:
        """Впечатать продиктованное в активное поле — голосовой ввод в фокус.

        Инструмент **не в каталоге модели**: его зовёт резолвер `verbatim` по
        приставке («впиши …»), отдавая весь хвост реплики дословно. Так диктовку
        не режут шаблоны и не усекает разбор — а это и было бедой первой версии.

        По умолчанию текст не набивается буквально, а **переписывается моделью**
        в аккуратное сообщение: продиктованное вслух звучит рвано, с оговорками
        и «э-э», и владелец ждёт, что ассистент причешет. Escape для буквального
        ввода — начать с «дословно»: «впиши дословно …». Enter не жмём в любом
        случае: вписать — наше дело, отправлять решает человек.

        :param text: что впечатать; весь хвост реплики после приставки.
        """
        body = text.strip()
        if not body:
            return ToolResult.failure(
                "нечего вписывать",
                speech={"ru": "Что вписать, сэр?", "en": "What should I type?"},
            )
        if sys.platform != "win32":
            return ToolResult.failure(
                "ввод текста доступен только на Windows",
                speech={"ru": "Вписывать текст я умею только на Windows.",
                        "en": "I can only type text on Windows."},
            )

        literal, body = strip_verbatim_marker(body)
        if self._rewrite and not literal and self.context.llm.available:
            body = await self._polish(body)

        try:
            sent = await asyncio.to_thread(type_text_os, body)
        except OSError as error:
            self.log.warning("Не удалось впечатать текст: %s", error)
            return ToolResult.failure(
                f"ввод текста не прошёл: {error}",
                speech={"ru": "Не получилось вписать.", "en": "I couldn't type it."},
            )
        self.log.info("Вписал %d символов", len(body))
        return ToolResult.success(
            {"typed": body, "events": sent},
            speech={"ru": ("Готово, сэр.", "Вписал.", "Готово."),
                    "en": ("Done, sir.", "Typed it.")},
        )

    async def _polish(self, body: str) -> str:
        """Переписать продиктованное в аккуратный текст. Сбой — вернуть как есть.

        Печатать нечего, если модель промолчала или упала, поэтому любой сбой —
        это буквальный ввод, а не пустое поле: продиктованное дороже красоты.
        """
        try:
            polished = await self.context.llm.ask(body, task="dialog", system=_POLISH)
        except LLMErrors as error:
            self.log.warning("Причесать текст не вышло, впишу дословно: %s", error)
            return body
        clean = polished.strip().strip("«»\"'").strip()
        if clean and clean != body:
            self.log.info("Причесал: %r -> %r", body, clean)
        return clean or body

    async def health(self) -> HealthStatus:
        """Здоровье: на своей платформе и включённый — должен и следить."""
        if sys.platform != "win32":
            return HealthStatus.degraded("только Windows")
        if self._enabled and (self._watcher is None or not self._watcher.running):
            return HealthStatus.degraded("включён, но хук не встал")
        state = "следит" if self._enabled else "выключен"
        return HealthStatus.healthy(state)
