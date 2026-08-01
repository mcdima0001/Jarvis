"""Подготовка текста к произнесению русским голосом.

Голос читает текст по правилам своего языка, поэтому чужой алфавит нужно
перевести. Piper отдаёт текст в espeak-ng с русскими правилами, и латиница по
ним читается как каша: «OpenRouter» звучит так, что распознаётся обратно как
«об англутов», а «.env» — как один невнятный звук.

Лечится не переписыванием реплик в скиллах: латиница попадает в речь ещё и
подстановками — именами скиллов, `steam.exe`, идентификаторами моделей вида
`anthropic/claude-sonnet-5`. Поэтому нормализация живёт здесь, в слое синтеза,
и работает сразу для всех.

Второе, ради чего этот модуль существует: **у нейросетевых движков конечный
алфавит**. У модели Vosk в нём 63 символа — кириллица и горстка знаков
препинания. Ни цифр, ни кавычек-ёлочек, ни даже обычной двойной кавычки там
нет, и любой посторонний символ роняет синтез с `KeyError`. А текст к нам
приходит от LLM, то есть содержать может что угодно. Поэтому в конце стоит
белый список: всё, чего в нём нет, до движка не доходит.

Порядок: словарь известных названий (дополняется в конфиге) → транслитерация
остатка → числа словами → фильтр по белому списку.
"""

from __future__ import annotations

import re
import unicodedata
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


#: Типографика, у которой есть безопасный эквивалент. Кавычки убираем совсем:
#: вслух они не звучат, а в алфавите движка их нет.
_TYPOGRAPHY: dict[str, str] = {
    "«": "", "»": "", "„": "", "“": "", "”": "", "‘": "", "’": "'", '"': "",
    "—": " - ", "–": "-", "‑": "-", "…": "...", " ": " ", " ": " ",
    "№": " номер ", "§": " параграф ", "&": " и ", "+": " плюс ",
    "°": " градус ", "%": " процент ", "€": " евро ", "$": " доллар ", "₽": " рубль ",
}

#: Валюта пишется перед числом, а произносится после: $5 -> пять долларов.
#: Число забираем целиком, иначе «$10» превращается в «один доллар ноль».
_CURRENCY_BEFORE = re.compile(r"([$€₽])\s*(\d+(?:[.,]\d+)?)")
_CURRENCY_NAMES = {"$": "доллар", "€": "евро", "₽": "рубль"}

#: Что разрешено произносить русским голосом. Всё прочее выкидывается:
#: посторонний символ роняет синтез, а не читается как пауза.
_ALLOWED_RU = re.compile(r"[^А-Яа-яЁё0-9 .,!?;:()\-']+")

_ONES = ("ноль", "один", "два", "три", "четыре",
         "пять", "шесть", "семь", "восемь", "девять")
_ONES_FEMALE = ("ноль", "одна", "две", "три", "четыре",
                "пять", "шесть", "семь", "восемь", "девять")
_TEENS = ("десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать",
          "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать")
_TENS = ("", "", "двадцать", "тридцать", "сорок", "пятьдесят",
         "шестьдесят", "семьдесят", "восемьдесят", "девяносто")
_HUNDREDS = ("", "сто", "двести", "триста", "четыреста",
             "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот")

#: Разряды: имя в трёх формах и род (тысяча — женского).
_SCALES: tuple[tuple[tuple[str, str, str], bool], ...] = (
    (("", "", ""), False),
    (("тысяча", "тысячи", "тысяч"), True),
    (("миллион", "миллиона", "миллионов"), False),
    (("миллиард", "миллиарда", "миллиардов"), False),
    (("триллион", "триллиона", "триллионов"), False),
)

#: Единицы, которые появляются в речи чаще всего, — с согласованием по числу.
_UNITS: dict[str, tuple[str, str, str]] = {
    "процент": ("процент", "процента", "процентов"),
    "градус": ("градус", "градуса", "градусов"),
    "рубль": ("рубль", "рубля", "рублей"),
    "доллар": ("доллар", "доллара", "долларов"),
    "евро": ("евро", "евро", "евро"),
    "номер": ("номер", "номер", "номер"),
}

_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")
#: Разряды, разделённые пробелом: «1 299» — одно число, а не два.
_GROUPED_DIGITS = re.compile(r"(\d)[\s  ](\d{3})\b")


def plural_form(count: int, forms: tuple[str, str, str]) -> str:
    """Выбрать форму слова под число: 1 час, 2 часа, 5 часов.

    :param count: число, к которому относится слово.
    :param forms: формы для 1, 2 и 5.
    """
    count = abs(count) % 100
    if 11 <= count <= 14:
        return forms[2]
    remainder = count % 10
    if remainder == 1:
        return forms[0]
    if 2 <= remainder <= 4:
        return forms[1]
    return forms[2]


def _triple_to_words(value: int, *, female: bool) -> list[str]:
    """Прочитать группу из трёх цифр."""
    words: list[str] = []
    if hundreds := value // 100:
        words.append(_HUNDREDS[hundreds])
    remainder = value % 100
    if 10 <= remainder <= 19:
        words.append(_TEENS[remainder - 10])
    else:
        if tens := remainder // 10:
            words.append(_TENS[tens])
        if ones := remainder % 10:
            words.append(_ONES_FEMALE[ones] if female else _ONES[ones])
    return words


def number_to_words(value: int, *, female: bool = False) -> str:
    """Записать целое число словами.

    Нужно не для красоты: у модели Vosk в алфавите нет цифр, и «22» роняет
    синтез так же, как кавычка-ёлочка.

    :param female: читать единицы в женском роде — «двадцать одна минута»
        вместо «двадцать один минута». Разряды свой род знают сами (тысяча
        женского рода всегда), а вот последняя группа зависит от того, к чему
        число относится, и по самому числу этого не узнать.
    """
    if value == 0:
        return _ONES[0]

    words: list[str] = []
    if value < 0:
        words.append("минус")
        value = -value

    groups: list[int] = []
    while value:
        groups.append(value % 1000)
        value //= 1000

    if len(groups) > len(_SCALES):
        # Дальше триллионов не считаем: вслух такие числа всё равно не нужны.
        return " ".join(_ONES[int(digit)] for digit in str(value))

    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if not group:
            continue
        forms, scale_female = _SCALES[index]
        # Род последней группы задаёт не разряд, а то существительное, которое
        # идёт следом: «двадцать одна минута», но «двадцать один градус».
        words.extend(
            _triple_to_words(group, female=scale_female or (female and index == 0))
        )
        if forms[0]:
            words.append(plural_form(group, forms))

    return " ".join(words)


#: Порядковые: только последняя значащая часть числа меняет форму.
#: 47-й -> «сорок седьмой», 2026-й -> «две тысячи двадцать шестой».
_ORDINAL_ONES = ("", "первый", "второй", "третий", "четвёртый", "пятый",
                 "шестой", "седьмой", "восьмой", "девятый")
_ORDINAL_TEENS = ("десятый", "одиннадцатый", "двенадцатый", "тринадцатый",
                  "четырнадцатый", "пятнадцатый", "шестнадцатый",
                  "семнадцатый", "восемнадцатый", "девятнадцатый")
_ORDINAL_TENS = ("", "", "двадцатый", "тридцатый", "сороковой", "пятидесятый",
                 "шестидесятый", "семидесятый", "восьмидесятый", "девяностый")
_ORDINAL_HUNDREDS = ("", "сотый", "двухсотый", "трёхсотый", "четырёхсотый",
                     "пятисотый", "шестисотый", "семисотый", "восьмисотый",
                     "девятисотый")
_ORDINAL_SCALES = ("", "тысячный", "миллионный", "миллиардный", "триллионный")
#: Круглые разряды сращиваются в одно слово: двухтысячный, пятимиллионный.
_ORDINAL_PREFIX = ("", "", "двух", "трёх", "четырёх", "пяти",
                   "шести", "семи", "восьми", "девяти")
#: Род берётся из самого текста: «29-е» — среднего, «2-я» — женского.
_ORDINAL_GENDER: dict[str, tuple[str, str]] = {
    "я": ("ая", "ья"), "ая": ("ая", "ья"), "ю": ("ую", "ью"),
    "е": ("ое", "ье"), "ое": ("ое", "ье"),
}

#: Хвост порядкового числительного: 47-й, 5-я, 2-го, 10-му.
_ORDINAL = re.compile(r"(\d+)-(й|я|е|го|му|м|ю|ой|ым|ых|ые|ая|ое)\b")


def ordinal_to_words(value: int, *, ending: str = "") -> str:
    """Записать порядковое числительное словами.

    Меняется только последняя значащая часть: «сорок семь» → «сорок седьмой»,
    а всё, что левее, остаётся количественным.

    :param ending: окончание из текста («29-е», «2-я»). По нему выбирается род:
        падежи не разбираем, но средний и женский встречаются постоянно.
    """
    if value <= 0:
        return number_to_words(value)

    remainder = value % 100
    if 10 <= remainder <= 19:
        head, tail = value - remainder, _ORDINAL_TEENS[remainder - 10]
    elif ones := value % 10:
        head, tail = value - ones, _ORDINAL_ONES[ones]
    elif tens := (value % 100) // 10:
        head, tail = value - tens * 10, _ORDINAL_TENS[tens]
    elif hundreds := (value % 1000) // 100:
        head, tail = value - hundreds * 100, _ORDINAL_HUNDREDS[hundreds]
    else:
        # Круглый разряд: тысячный, миллионный. Считаем, сколько нулей.
        scale = 0
        rest = value
        while rest % 1000 == 0 and scale + 1 < len(_ORDINAL_SCALES):
            rest //= 1000
            scale += 1
        if scale and rest == 1:
            return _ORDINAL_SCALES[scale]
        if scale and rest < len(_ORDINAL_PREFIX):
            return _ORDINAL_PREFIX[rest] + _ORDINAL_SCALES[scale]
        return f"{number_to_words(rest)} {_ORDINAL_SCALES[scale]}".strip()

    if (forms := _ORDINAL_GENDER.get(ending)) and tail:
        # «третий» склоняется иначе остальных — для него своя форма.
        tail = tail[:-2] + (forms[1] if tail.endswith("ий") else forms[0])

    prefix = number_to_words(head) if head else ""
    return f"{prefix} {tail}".strip()


def _spell_ordinal(match: re.Match[str]) -> str:
    """Заменить «47-й» словами."""
    return ordinal_to_words(int(match.group(1)), ending=match.group(2))


#: Существительные женского рода, которые в речи ассистента стоят при числах.
#: «21 минут» голос читает как «двадцать один минут» — и это слышно сразу.
#: Список короткий намеренно: сюда попадает только то, что мы правда говорим.
_FEMALE_NOUNS = ("минут", "секунд", "недел", "тысяч", "сотн", "тонн")


def _following_noun(text: str, at: int) -> str:
    """Слово сразу за числом; пусто, если там не слово."""
    tail = text[at:]
    if not tail[:1].isspace():
        return ""
    match = re.match(r"\s+([^\W\d_]+)", tail)
    return match.group(1).lower() if match else ""


def _spell_number(match: re.Match[str]) -> str:
    """Заменить найденное число его словесной записью."""
    raw = match.group(0)
    whole, _, fraction = raw.replace(",", ".").partition(".")

    # Заглядываем на слово вперёд: род и падеж числительного задаёт оно.
    noun = _following_noun(match.string, match.end())
    female = any(noun.startswith(stem) for stem in _FEMALE_NOUNS)
    words = number_to_words(int(whole), female=female)
    # Винительный падеж отличается от именительного ровно в одном слове:
    # «прошла одна минута», но «подожди одну минуту». Само существительное уже
    # стоит в нужной форме — по нему и определяем.
    if female and words.endswith("одна") and noun.endswith(("у", "ю")):
        words = words[: -len("одна")] + "одну"
    if fraction:
        # «22.5» читаем как «двадцать два запятая пять» — так же, как говорят.
        words += " запятая " + " ".join(_ONES[int(digit)] for digit in fraction)
    return words


def _agree_units(text: str) -> str:
    """Согласовать единицы измерения с числом: 1 процент, 5 процентов."""

    def replace(match: re.Match[str]) -> str:
        number, unit = match.group(1), match.group(2)
        forms = _UNITS[unit]
        whole = int(number.replace(",", ".").split(".")[0])
        return f"{number} {plural_form(whole, forms)}"

    pattern = re.compile(rf"(-?\d+(?:[.,]\d+)?)\s+({'|'.join(_UNITS)})\b")
    return pattern.sub(replace, text)


def _sanitize(text: str) -> str:
    """Оставить только то, что движок способен произнести."""
    # Знаки ударения (Ви́ки пишет их постоянно) — отдельные символы поверх
    # буквы. Если просто выкинуть их как посторонние, слово разорвётся
    # пробелом: «До́нальд» превратится в «До нальд». Сначала собираем
    # составные буквы обратно в единые (иначе потеряется «ё»), потом убираем
    # то, что осталось висеть сверху.
    text = "".join(
        char
        for char in unicodedata.normalize("NFC", text)
        if not unicodedata.combining(char)
    )
    text = _ALLOWED_RU.sub(" ", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+([.,!?;:)])", r"\1", text)
    return text.strip()


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

    text = _CURRENCY_BEFORE.sub(lambda m: f"{m.group(2)} {_CURRENCY_NAMES[m.group(1)]}", text)
    for source, target in _TYPOGRAPHY.items():
        text = text.replace(source, target)

    if not language.startswith("ru"):
        # Английский (или любой другой латинский) голос: убираем кириллицу.
        # Цифры и знаки препинания он читает сам, чистить их не нужно.
        result = _CYRILLIC_RUN.sub(lambda m: romanize(m.group(0)), text)
        return re.sub(r"\s{2,}", " ", result).strip()

    dictionary = dict(DEFAULT_PRONUNCIATION)
    if pronunciation:
        dictionary.update({key.lower(): value for key, value in pronunciation.items()})

    result = _LATIN_RUN.sub(lambda m: _speak_token(m.group(0), dictionary), text)
    # Единицы согласуются до превращения чисел в слова: «5 процент» проще
    # исправить, пока число ещё число.
    while _GROUPED_DIGITS.search(result):
        result = _GROUPED_DIGITS.sub(r"\1\2", result)
    result = _agree_units(result)
    # Порядковые разбираем раньше количественных: иначе «47-й» успевает стать
    # «сорок семь-й».
    result = _ORDINAL.sub(_spell_ordinal, result)
    result = _NUMBER.sub(_spell_number, result)
    return _sanitize(result)
