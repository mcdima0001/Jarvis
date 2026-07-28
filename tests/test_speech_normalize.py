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
