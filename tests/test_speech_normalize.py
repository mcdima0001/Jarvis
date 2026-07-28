"""Подготовка текста к синтезу русским голосом.

Проверено на живом Piper: латиница по русским правилам espeak читается как
каша. «Ключ OpenRouter в файл .env» распознавалось обратно как «ключ об
англутов в файл n» — отсюда все проверки ниже.
"""

from __future__ import annotations

import re

from jarvis.core.tts.normalize import normalize_for_speech, transliterate

_LATIN = re.compile(r"[A-Za-z]")


def test_known_names_get_russian_reading() -> None:
    """Названия из словаря читаются по-человечески."""
    assert "Опен Раутер" in normalize_for_speech("Ключ OpenRouter готов")
    assert "Телеграм" in normalize_for_speech("Открываю Telegram")
    assert "Ютуб" in normalize_for_speech("Ищу на YouTube")


def test_file_extension_is_dropped() -> None:
    """Расширение вслух не нужно: «Стим», а не «Стим ексе»."""
    assert normalize_for_speech("Запускаю steam.exe.") == "Запускаю Стим."


def test_sentence_period_is_not_eaten() -> None:
    """Точка в конце предложения остаётся на месте."""
    assert normalize_for_speech("Готово. Steam закрыт.").endswith("закрыт.")


def test_leading_dot_name() -> None:
    """`.env` не превращается в «в.конфигурация»."""
    result = normalize_for_speech("Ключ лежит в .env")
    assert "." not in result.replace("в конфигурация", ""), result
    assert "конфигурация" in result


def test_path_like_identifier_loses_separators() -> None:
    """Слэши и дефисы не читаются вслух как «черта»."""
    result = normalize_for_speech("Используется anthropic/claude-sonnet-5.")
    assert "/" not in result
    assert "Антропик" in result and "Клод" in result


def test_abbreviation_is_spelled_out() -> None:
    """Короткие аббревиатуры произносятся по буквам."""
    assert "О-Би-Эс" in normalize_for_speech("Открываю OBS")


def test_unknown_latin_is_transliterated() -> None:
    """Незнакомое латинское слово всё равно становится произносимым."""
    result = normalize_for_speech("Открываю Reaper")
    assert not _LATIN.search(result), f"латиница осталась: {result}"


def test_custom_dictionary_wins() -> None:
    """Словарь из конфига дополняет встроенный."""
    result = normalize_for_speech("Открываю Ableton", {"Ableton": "Эйблтон"})
    assert "Эйблтон" in result


def test_russian_text_untouched() -> None:
    """Чистая русская реплика не меняется."""
    text = "Языковая модель не подключена. Добавь ключ в настройки."
    assert normalize_for_speech(text) == text


def test_transliteration_handles_digraphs() -> None:
    """Диграфы разбираются раньше одиночных букв."""
    assert transliterate("shift") == "шифт"
    assert transliterate("church") == "чурч"


def test_empty_input() -> None:
    """Пустая строка не ломает нормализацию."""
    assert normalize_for_speech("") == ""


# --- английский голос -------------------------------------------------------


def test_cyrillic_romanized_for_english_voice() -> None:
    """Английскому голосу кириллицу нужно записать латиницей.

    Иначе он читает её по своим правилам и получается такая же каша, как у
    русского голоса на латинице.
    """
    result = normalize_for_speech("Режим game включён", language="en")
    assert "Rezhim" in result
    assert "game" in result


def test_english_text_untouched_by_english_voice() -> None:
    """Чистый английский текст английскому голосу менять не нужно."""
    text = "Light in studio is on."
    assert normalize_for_speech(text, language="en") == text


def test_latin_kept_for_english_voice() -> None:
    """Названия программ английскому голосу транслитерировать не надо."""
    assert "OBS" in normalize_for_speech("Opening OBS now", language="en")


# --- алфавит движка ---------------------------------------------------------


def test_typographic_quotes_removed() -> None:
    """Кавычки-ёлочки роняли синтез: их нет в алфавите модели Vosk.

    Реальный сбой: реплика «Не нашёл ничего по запросу «...»» падала с
    KeyError: '»', и ассистент молчал на каждом неудачном поиске.
    """
    result = normalize_for_speech("Не нашёл ничего по запросу «погода в Москве».")
    assert "«" not in result and "»" not in result
    assert "погода в Москве" in result


def test_digits_spelled_out() -> None:
    """Цифр в алфавите модели тоже нет — любое число нужно записать словами."""
    assert normalize_for_speech("В студии 22 градуса") == "В студии двадцать два градуса"
    assert "ноль" in normalize_for_speech("Осталось 0 попыток")


def test_units_agree_with_number() -> None:
    """Единицы склоняются по числу: 1 процент, 3 процента, 5 процентов."""
    assert "один процент" in normalize_for_speech("яркость 1 процент")
    assert "три процента" in normalize_for_speech("яркость 3 процент")
    assert "пять процентов" in normalize_for_speech("яркость 5 процент")


def test_percent_sign_becomes_word() -> None:
    """Знак процента произносится и согласуется как слово."""
    assert normalize_for_speech("яркость 100%") == "яркость сто процентов"


def test_ordinals_read_as_ordinals() -> None:
    """«47-й президент», а не «сорок семь-й президент»."""
    assert normalize_for_speech("47-й президент") == "сорок седьмой президент"
    assert normalize_for_speech("Сегодня 29-е") == "Сегодня двадцать девятое"
    assert normalize_for_speech("3-я попытка") == "третья попытка"


def test_stress_marks_do_not_split_words() -> None:
    """Ударения из Википедии — отдельные символы поверх буквы.

    Выкинуть их как посторонние нельзя: слово разорвётся пробелом и
    «До́нальд» превратится в «До нальд».
    """
    assert normalize_for_speech("До́нальд Трамп") == "Дональд Трамп"
    assert normalize_for_speech("Всё чётко") == "Всё чётко"


def test_unknown_symbols_dropped() -> None:
    """Эмодзи и стрелки от LLM до движка доходить не должны."""
    result = normalize_for_speech("Готово 🔥 → дальше")
    assert result == "Готово дальше"


def test_thousands_separator_is_one_number() -> None:
    """«1 299» — одно число, а не «один» и «двести девяносто девять»."""
    assert normalize_for_speech("1 299 рублей").startswith("одна тысяча двести")


def test_currency_read_after_number() -> None:
    """Знак валюты пишется перед числом, а произносится после."""
    assert normalize_for_speech("подписка $10") == "подписка десять долларов"


def test_english_voice_keeps_digits() -> None:
    """Английскому движку цифры чистить не надо — он их читает сам."""
    assert normalize_for_speech("22 degrees", language="en") == "22 degrees"
