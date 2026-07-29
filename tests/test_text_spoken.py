"""Сравнение услышанного с написанным: алфавиты, произношение, разделители.

Ядро этой задачи одно на весь проект: голос всегда встречается с текстом,
который писали не мы. Раньше эти функции жили в скилле windows, а нужны
оказались ещё браузеру — и для программ, и для сайтов, и для заголовков
вкладок.
"""

from __future__ import annotations

import pytest

from jarvis.core.text import romanize, skeleton, squash


@pytest.mark.parametrize(
    "text, expected",
    [
        ("OBS Studio", "obsstudio"),
        ("obs-studio", "obsstudio"),
        ("Яндекс.Музыка", "яндексмузыка"),
        ("Яндекс Музыка", "яндексмузыка"),
        ("  ОБС  ", "обс"),
    ],
)
def test_separators_are_dropped(text: str, expected: str) -> None:
    """Пробелы и знаки внутри названия расставлены как попало."""
    assert squash(text) == expected


def test_cyrillic_written_in_latin() -> None:
    """Нечёткое сравнение между алфавитами бесполезно — сначала один алфавит."""
    assert romanize("обс") == "obs"
    assert romanize("джарвис") == "dzharvis"


@pytest.mark.parametrize(
    "heard, written",
    [
        ("MarshallTech", "МаршалТех"),      # то же слово, другой алфавит
        ("Marshall Tech", "МаршалТех"),     # и другие разделители
        ("фотошоп", "photoshop"),           # ph читается как ф
        ("хром", "chrome"),                 # ch читается как х
        ("зум", "zoom"),                    # двойная гласная
        ("дискорд", "Discord"),             # двойная согласная
        ("ноушен", "notion"),               # tion читается как шн
        ("файрфокс", "firefox"),            # x читается как кс
    ],
)
def test_same_sound_same_skeleton(heard: str, written: str) -> None:
    """Гласные на слух плывут, согласные остаются.

    Побуквенная транслитерация тут не спасает: «фотошоп» и «photoshop»
    совпадают меньше чем наполовину.
    """
    assert skeleton(heard) == skeleton(written)


@pytest.mark.parametrize(
    "first, second",
    [("борщ", "MarshallTech"), ("ютуб", "яндекс"), ("стим", "телеграм")],
)
def test_different_words_differ(first: str, second: str) -> None:
    """Костяк огрубляет слово, но не до неразличимости."""
    assert skeleton(first) != skeleton(second)


def test_skeleton_of_a_short_word_is_short() -> None:
    """У «YouTube» костяк равен «tb» — по нему искать нельзя, и это видно."""
    assert skeleton("YouTube") == "tb"
    assert len(skeleton("ютуб")) == 2
