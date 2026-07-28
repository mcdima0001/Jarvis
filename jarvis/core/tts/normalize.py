"""Подготовка текста к произнесению русским голосом.

Piper отдаёт текст в espeak-ng с правилами русского языка. Латиница по этим
правилам читается как каша: «OpenRouter» звучит так, что распознаётся обратно
как «об англутов», а «.env» — как один невнятный звук.

Лечится не переписыванием реплик в скиллах: латиница попадает в речь ещё и
подстановками — именами скиллов, `steam.exe`, идентификаторами моделей вида
`anthropic/claude-sonnet-5`. Поэтому нормализация живёт здесь, в слое синтеза,
и работает сразу для всех.

Порядок такой: сперва словарь известных названий (его можно дополнять в
конфиге), затем побуквенная транслитерация всего, что осталось.
"""

from __future__ import annotations

import re
from typing import Mapping

#: Названия, у которых есть устоявшееся русское чтение.
DEFAULT_PRONUNCIATION: dict[str, str] = {
    "openrouter": "Опен Раутер",
    "telegram": "Телеграм",
    "youtube": "Ютуб",
    "steam": "Стим",
    "obs": "О-Би-Эс",
    "esp32": "ЕСП тридцать два",
    "xlr": "Икс-Эль-Эр",
    "whisper": "Виспер",
    "piper": "Пайпер",
    "jarvis": "Джарвис",
    "wifi": "Вай-Фай",
    "wi-fi": "Вай-Фай",
    "usb": "Ю-Эс-Би",
    "hdmi": "Эйч-Ди-Эм-Ай",
    "api": "А-Пи-Ай",
    "url": "Ю-Эр-Эль",
    "env": "конфигурация",
    "anthropic": "Антропик",
    "claude": "Клод",
    "sonnet": "Соннет",
    "opus": "Опус",
    "haiku": "Хайку",
    "google": "Гугл",
    "chrome": "Хром",
    "windows": "Виндоус",
    "explorer": "Проводник",
    # Названия задач из конфига: попадают в речь через core.set_model.
    "dialog": "диалог",
    "code": "код",
    "summarize": "пересказ",
    "intent": "разбор команд",
    "analysis": "анализ",
}

#: Диграфы разбираются раньше одиночных букв, иначе «sh» станет «сх».
_DIGRAPHS: tuple[tuple[str, str], ...] = (
    ("sch", "ш"), ("sh", "ш"), ("ch", "ч"), ("ph", "ф"), ("th", "т"),
    ("ck", "к"), ("qu", "кв"), ("ee", "и"), ("oo", "у"), ("ea", "и"),
    ("ai", "эй"), ("ay", "эй"), ("ey", "эй"), ("oy", "ой"), ("ju", "джу"),
    ("ya", "я"), ("yu", "ю"), ("yo", "ё"),
)

_LETTERS: dict[str, str] = {
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г",
    "h": "х", "i": "и", "j": "дж", "k": "к", "l": "л", "m": "м", "n": "н",
    "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у",
    "v": "в", "w": "в", "x": "кс", "y": "и", "z": "з",
}

# Составные идентификаторы (steam.exe, anthropic/claude-sonnet-5) ловим целиком,
# но точку в конце предложения не захватываем: после разделителя обязана идти
# буква или цифра. Иначе «Запускаю steam.exe.» теряет расширение из виду и
# читается как «Стим ексе».
_LATIN_RUN = re.compile(r"\.?[A-Za-z][A-Za-z0-9_-]*(?:[./][A-Za-z0-9_-]+)*")
#: Расширения и служебные хвосты, которые вслух не нужны.
_NOISE_SUFFIX = re.compile(r"\.(exe|py|json|onnx|yaml|yml|md|txt|log)$", re.IGNORECASE)


def transliterate(word: str) -> str:
    """Прочитать латинское слово русскими буквами."""
    lowered = word.lower()
    for source, target in _DIGRAPHS:
        lowered = lowered.replace(source, target)
    return "".join(_LETTERS.get(char, char) for char in lowered)


def _speak_token(token: str, dictionary: Mapping[str, str]) -> str:
    """Превратить один латинский фрагмент в произносимый вид."""
    # Имена вида .env приходят с ведущей точкой — вслух она не нужна.
    token = _NOISE_SUFFIX.sub("", token.lstrip("."))
    if not token:
        return ""

    known = dictionary.get(token.lower())
    if known:
        return known

    # Составные вроде anthropic/claude-sonnet-5 или steam.exe разбираем по частям.
    parts = [part for part in re.split(r"[./_-]+", token) if part]
    if len(parts) > 1:
        return " ".join(_speak_token(part, dictionary) for part in parts).strip()

    if token.isdigit():
        return token

    # Аббревиатуры до четырёх букв читаем по буквам: OBS -> О-Би-Эс звучит
    # понятнее, чем «обс».
    if token.isupper() and len(token) <= 4:
        alphabet = {
            "a": "Эй", "b": "Би", "c": "Си", "d": "Ди", "e": "И", "f": "Эф",
            "g": "Джи", "h": "Эйч", "i": "Ай", "j": "Джей", "k": "Кей",
            "l": "Эль", "m": "Эм", "n": "Эн", "o": "О", "p": "Пи", "q": "Кью",
            "r": "Эр", "s": "Эс", "t": "Ти", "u": "Ю", "v": "Ви", "w": "Дабл-Ю",
            "x": "Икс", "y": "Уай", "z": "Зет",
        }
        return "-".join(alphabet.get(char.lower(), char) for char in token)

    return transliterate(token)


#: Обратное направление: кириллица для английского голоса.
_CYRILLIC: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

_CYRILLIC_RUN = re.compile(r"[А-Яа-яЁё]+")


def romanize(word: str) -> str:
    """Записать русское слово латиницей, чтобы английский голос его прочитал.

    Регистр сохраняется: имена собственные в середине фразы не должны
    превращаться в строчные.
    """
    result: list[str] = []
    for char in word:
        replacement = _CYRILLIC.get(char.lower())
        if replacement is None:
            result.append(char)
        elif char.isupper():
            result.append(replacement.capitalize())
        else:
            result.append(replacement)
    return "".join(result)


def normalize_for_speech(
    text: str,
    pronunciation: Mapping[str, str] | None = None,
    *,
    language: str = "ru",
) -> str:
    """Подготовить текст к синтезу голосом конкретного языка.

    Голос читает текст по правилам своего языка, поэтому чужой алфавит нужно
    перевести: русскому голосу — латиницу в кириллицу, английскому — наоборот.
    Иначе вместо слов получается каша.

    :param text: исходная реплика, возможно с латиницей и путями.
    :param pronunciation: дополнения к словарю из конфига ``tts.pronounce``.
    :param language: язык голоса, которым будет произнесён текст.
    """
    if not text:
        return text

    if not language.startswith("ru"):
        # Английский (или любой другой латинский) голос: убираем кириллицу.
        return _CYRILLIC_RUN.sub(lambda m: romanize(m.group(0)), text)

    dictionary = dict(DEFAULT_PRONUNCIATION)
    if pronunciation:
        dictionary.update({key.lower(): value for key, value in pronunciation.items()})

    result = _LATIN_RUN.sub(lambda m: _speak_token(m.group(0), dictionary), text)
    # После вырезания расширений могли остаться двойные пробелы и висящие точки.
    result = re.sub(r"\s{2,}", " ", result)
    result = re.sub(r"\s+([.,!?;:])", r"\1", result)
    return result.strip()
