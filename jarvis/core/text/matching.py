"""Лестница сопоставления: услышанное против списка написанного.

Задача одна и та же во всём проекте: человек назвал что-то вслух, а у нас есть
список названий, которые писали не мы. Программа в меню «Пуск», сайт в конфиге,
чат в Telegram, подпись кнопки на странице — источники разные, а работа
одинаковая, и до 04.09.2026 она была написана **пять раз**: в `windows`,
`browser`, `page`, `telegram` и почти-так-же в резолвере синонимов. Пороги при
этом разъехались (0.6, 0.66, 0.8, 0.82), а правила «совпало краем» и «костяк
короче N не берём» жили каждое своей жизнью — хотя выстраданы были общие.

**Лестница из четырёх ступеней, от точного к грубому:**

1. **точное совпадение** любой формы написания (:func:`forms`) — падеж,
   алфавит, разделители уже учтены;
2. **совпадение краем** (:func:`touches`) — «обс» находит «OBS Studio»,
   «торрент» — «qBittorrent». Именно краем, а не любым куском: «telegramdesktop»
   содержит «кто», и вопрос «кто такой трамп» открывал Telegram;
3. **согласный костяк** (:func:`sounds_alike`) — «фотошоп» и «photoshop»
   пишутся по-разному, а звучат одинаково;
4. **нечёткое сравнение** (:func:`closeness`) — последняя попытка, с порогом.

Порядок важен и обратным быть не может: короткий запрос при обратном порядке
цепляет случайного соседа по алфавиту.

**Пороги остаются у вызывающего, и это не недоделка.** Цена ошибки везде
разная: не тот чат — сообщение уходит чужому человеку, не та программа —
запрос прав администратора, не тот сайт — открылась лишняя вкладка. Общим тут
может быть алгоритм, но не строгость; поэтому `similarity` спрашивается
обязательным аргументом, а не подставляется по умолчанию.
"""

from __future__ import annotations

import difflib
import re
from typing import Callable, Iterable, Sequence

from .spoken import romanize, skeleton, squash

#: Слова названия: всё, что не буква и не цифра, считается разделителем.
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)

#: Гласные на конце — по ним и различаются падежи: «маме», «мама», «маму»;
#: «на ютубе», «в гитхабе». Латинские тоже: услышанное пишут обоими алфавитами.
ENDINGS = "аеёиоуыэюяaeiouy"

#: Короче этого сравнивать началом бессмысленно: «ма» подойдёт к половине книги.
LEAST = 3

#: Костяк короче этого совпадёт со слишком многим: у «YouTube» он равен «tb».
LEAST_SKELETON = 4


def stem(text: str) -> str:
    """Отбросить окончание, чтобы падеж перестал мешать сравнению."""
    return text.rstrip(ENDINGS)


def forms(text: str, *, least: int = LEAST) -> set[str]:
    """Как одно и то же название может выглядеть.

    «Маме» и «Мама» — одно имя в разных падежах, «саша» и «Sasha» — в разных
    алфавитах, «Настя Ко» и «настяко» — с разделителем и без. Сравнивать
    поштучно каждый случай значит писать одно и то же четыре раза.

    :param least: короче скольких букв форму не брать.
    """
    tight = squash(text)
    latin = squash(romanize(text))
    found = {tight, stem(tight), latin, stem(latin)}
    return {form for form in found if len(form) >= least}


def touches(left: str, right: str, *, least: int = LEAST) -> bool:
    """Совпадают ли слова краем — началом или концом.

    Краем, а не любым куском, и это правило стоило двух разборов: «блокнот»
    содержит «окно», а «telegramdesktop» — «кто».
    """
    if len(left) < least or len(right) < least:
        return False
    short, long = sorted((left, right), key=len)
    return long.startswith(short) or long.endswith(short)


def starts(left: str, right: str, *, least: int = LEAST) -> bool:
    """Совпадают ли слова началом.

    Строже :func:`touches` на один конец. Нужно там, где хвост названия несёт
    смысл: «Яндекс Музыка» начинается с «яндекс», и по началу это правильное
    совпадение, а по концу «музыка» подошла бы к любому музыкальному сайту.
    """
    if len(left) < least or len(right) < least:
        return False
    short, long = sorted((left, right), key=len)
    return long.startswith(short)


def sounds_alike(left: str, right: str, *, least: int = LEAST_SKELETON) -> bool:
    """Одинаково ли звучат — по согласному костяку.

    Костяк сравнивается только на точное совпадение: приём и так грубый, а
    нечёткость поверх него даёт ложные попадания.
    """
    sounds = skeleton(left)
    return len(sounds) >= least and sounds == skeleton(right)


def closeness(left: str, right: str, *, balance: float = 0.0) -> float:
    """Похожесть двух написаний, от нуля до единицы.

    :param balance: насколько сопоставимы должны быть длины. Короткое слово
        иначе находит длинное по случайному совпадению букв: «окно» против
        «блокнот» даёт 0.73. Ноль — не проверять.
    """
    if not left or not right:
        return 0.0
    if balance and min(len(left), len(right)) / max(len(left), len(right)) < balance:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def best_match(
    query: str,
    candidates: Iterable[str],
    *,
    similarity: float,
    prefer: Callable[[str], object] = len,
    edges: Callable[..., bool] = touches,
    least: int = LEAST,
    least_skeleton: int = LEAST_SKELETON,
    balance: float = 0.0,
) -> str | None:
    """Найти среди кандидатов тот, который назвали вслух.

    :param query: как это произнесли.
    :param candidates: известные названия, как они написаны.
    :param similarity: порог нечёткого сравнения на последней ступени.
        Обязателен: цена ошибки у каждого вызывающего своя.
    :param prefer: чем меньше значение, тем лучше кандидат при равных правах.
        По умолчанию побеждает самое короткое имя: «мама» — это «Мама», а не
        «Мама Юли». Кому нужно наоборот, передаёт ``lambda name: -len(name)``:
        «Яндекс музыка» иначе проигрывает записи «Яндекс».
    :param edges: как сравнивать краем — :func:`touches` или :func:`starts`.
    :param least: короче скольких букв не сравнивать вовсе.
    :param least_skeleton: короче какого костяка не верить созвучию.
    :param balance: сопоставимость длин на нечёткой ступени.
    :return: название из ``candidates``, либо ``None``, если уверенности нет.
    """
    wanted = forms(query, least=least)
    if not wanted:
        return None

    known = [(name, forms(name, least=least)) for name in candidates]

    exact = [name for name, shapes in known if wanted & shapes]
    if exact:
        return min(exact, key=prefer)

    close = [
        name
        for name, shapes in known
        if any(edges(shape, part, least=least) for shape in shapes for part in wanted)
    ]
    if close:
        return min(close, key=prefer)

    alike = [
        name
        for name, _ in known
        if sounds_alike(query, name, least=least_skeleton)
    ]
    if alike:
        return min(alike, key=prefer)

    # Нечёткое — последняя попытка, и берётся лучшее совпадение из всех, а не
    # первое подошедшее: список кандидатов ничем не упорядочен.
    tight = squash(query)
    best: tuple[float, str] | None = None
    for name, _ in known:
        score = closeness(tight, squash(name), balance=balance)
        if score >= similarity and (best is None or score > best[0]):
            best = (score, name)
    return best[1] if best else None


def shared_word(left: str, right: str, *, least: int = 4) -> bool:
    """Есть ли у двух названий общее длинное слово.

    Родство, которого не видно при сравнении целиком: «логотип YouTube» и
    «YouTube Главная» говорят про одно и то же. Короткие слова не в счёт —
    предлоги и «для» есть у всех.
    """
    words = {squash(word) for word in _split(left)}
    asked = {squash(word) for word in _split(right)}
    return any(len(word) >= least for word in words & asked)


def _split(text: str) -> Sequence[str]:
    """Слова названия — по любым разделителям."""
    return _WORDS.findall(text.lower())
