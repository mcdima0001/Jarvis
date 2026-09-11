"""Зрение: подготовка снимка и сборка запроса к зрячей модели.

Экрана на сервере нет, поэтому проверяется всё, что можно посчитать без него:
сжатие, кодирование, выбор области и форма запроса. Сам захват остаётся тонкой
обёрткой над Pillow и проверяется живьём — ровно как у скилла `windows`.

Отдельно здесь стоит сторож контракта: **текстовое сообщение обязано уходить на
провод в прежней форме**. Картинки потребовали составного `content`, и соблазн
был перевести на него все сообщения разом. Текстовых запросов система делает
тысячи, а со зрением идут единицы, поэтому форма меняется только там, где без
этого нельзя.
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path
from typing import Any

from jarvis.core.config import load_config
from jarvis.core.llm import Message

_ROOT = Path(__file__).resolve().parent.parent


def _load() -> Any:
    """Загрузить скилл как модуль: он плагин и лежит вне пакета."""
    path = _ROOT / "skills" / "screen" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_screen", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


screen = _load()


# --- сжатие: размер снимка это и есть счёт ----------------------------------


def test_small_screenshot_is_not_stretched() -> None:
    """Маленькое не растягивается.

    Увеличение не добавляет ни одной разборчивой буквы, а платить за лишние
    пиксели пришлось бы по полной: модель считает их все.
    """
    assert screen.shrink((800, 600), limit=1600) == (800, 600)
    assert screen.shrink((1600, 900), limit=1600) == (1600, 900)


def test_large_screenshot_shrinks_keeping_shape() -> None:
    """Ужимается по длинной стороне, пропорции сохраняются."""
    assert screen.shrink((2560, 1440), limit=1600) == (1600, 900)
    # Вертикальный монитор: длинная сторона теперь другая.
    assert screen.shrink((1440, 2560), limit=1600) == (900, 1600)


def test_very_wide_shot_keeps_at_least_one_pixel() -> None:
    """Панорама из трёх мониторов не должна схлопнуть высоту в ноль."""
    width, height = screen.shrink((15360, 1080), limit=1600)
    assert width == 1600
    assert height >= 1


def test_default_limit_leaves_a_common_screen_alone() -> None:
    """Обычный экран не пересчитывается вовсе, а 4K ужимается.

    Предел стоит на родной ширине не ради экономии: замер показал, что цена
    картинки на этой модели одинакова на всём участке от 768 до 2200 пикселей.
    А вот читаемость от сжатия портится всерьёз — ужатый снимок дал уверенно
    неверный ответ. Поэтому пересчитывается только то, что заметно больше.
    """
    assert screen.shrink((1920, 1200)) == (1920, 1200)
    assert screen.shrink((3840, 2160)) == (1920, 1080)


def test_empty_size_is_not_a_crash() -> None:
    """Нулевой размер — не исключение, а нулевой размер."""
    assert screen.shrink((0, 0)) == (0, 0)
    assert screen.shrink((-5, 100)) == (0, 0)


# --- кодирование ------------------------------------------------------------


def test_data_uri_is_decodable_back() -> None:
    """Картинка доезжает до получателя байт в байт."""
    # Байтовый литерал только из ASCII: кириллица в нём — синтаксическая
    # ошибка, ровно как в заголовке HTTP-запроса.
    payload = b"\x89PNG\r\n\x1a\n not-a-real-screenshot"
    uri = screen.to_data_uri(payload)
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == payload


# --- о чём спрашивать -------------------------------------------------------


def test_empty_question_becomes_description_in_the_same_language() -> None:
    """Голое «посмотри на экран» — это просьба описать, а не молчание."""
    assert screen.question_for("", "ru") == "Что сейчас на экране?"
    assert screen.question_for("   ", "en") == "What is on the screen right now?"


def test_asked_question_wins() -> None:
    """Спросили конкретное — спрашиваем конкретное."""
    assert screen.question_for("  какая там ошибка  ", "ru") == "какая там ошибка"


# --- что снимать ------------------------------------------------------------


def test_known_targets_pass_through() -> None:
    """Три значения, которые объявлены в описании инструмента."""
    for target in ("screen", "window", "all"):
        assert screen.normalize_target(target) == target


def test_model_synonyms_are_understood() -> None:
    """Модель изобретает синонимы, и падать из-за этого глупо."""
    assert screen.normalize_target("окно") == "window"
    assert screen.normalize_target("Foreground") == "window"
    assert screen.normalize_target("мониторы") == "all"


def test_unknown_target_falls_back_to_one_monitor() -> None:
    """Непонятное — монитор с активным окном, самый безопасный вариант.

    Не «все экраны»: панорама из трёх мониторов после сжатия нечитаема, а
    заплатить за неё пришлось бы полностью.
    """
    assert screen.normalize_target("абракадабра") == "screen"
    assert screen.normalize_target("") == "screen"


# --- форма запроса ----------------------------------------------------------


def test_question_and_image_travel_in_one_message() -> None:
    """Вопрос и картинка — одна реплика: они про одно и то же."""
    messages = screen.build_messages("какая ошибка", "data:image/png;base64,AAAA", "ru")
    assert [message.role for message in messages] == ["system", "user"]
    assert messages[1].content == "какая ошибка"
    assert messages[1].images == ("data:image/png;base64,AAAA",)


def test_system_prompt_follows_the_language_of_the_question() -> None:
    """Русская подсказка на английском вопросе утащила бы и ответ в русский."""
    assert "aloud" in screen.build_messages("what is this", "x", "en")[0].content
    assert "вслух" in screen.build_messages("что это", "x", "ru")[0].content


def test_unknown_language_falls_back_to_russian() -> None:
    """Язык по умолчанию тут русский: на нём говорят с этим ассистентом."""
    assert "вслух" in screen.build_messages("?", "x", "de")[0].content


# --- сторож контракта -------------------------------------------------------


def test_message_without_images_is_unchanged_on_the_wire() -> None:
    """Главная проверка всей затеи со зрением.

    Составной `content` провайдеры понимают, но проверен он у нас только на
    зрении. Тысячи текстовых запросов обязаны уходить ровно тем же словарём,
    что и до появления картинок.
    """
    assert Message.user("привет").as_dict() == {"role": "user", "content": "привет"}
    assert Message.system("ты робот").as_dict() == {
        "role": "system",
        "content": "ты робот",
    }


def test_message_with_images_becomes_parts() -> None:
    """С картинкой форма составная: текст первой частью, картинки следом."""
    payload = Message.user("что тут", images=("data:image/png;base64,AAAA",)).as_dict()
    assert payload["role"] == "user"
    assert payload["content"] == [
        {"type": "text", "text": "что тут"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]


def test_several_images_keep_their_order() -> None:
    """Порядок картинок значим: «сравни это и вот это»."""
    payload = Message.user("сравни", images=("первая", "вторая")).as_dict()
    urls = [part["image_url"]["url"] for part in payload["content"][1:]]
    assert urls == ["первая", "вторая"]


# --- настройки --------------------------------------------------------------


def test_shipped_config_has_the_vision_profile() -> None:
    """Без профиля `vision` зрение не работает вовсе.

    Профиль отдельный потому, что модель обязана уметь картинки, а разбор
    команд идёт на самой дешёвой. Проверяется именно рабочий конфиг: забыть
    строку при переносе настроек — самый вероятный способ сломать зрение.
    """
    config = load_config(_ROOT / "config" / "config.yaml")
    assert screen.VISION_TASK in config.llm.profiles


def test_log_line_says_size_and_weight() -> None:
    """В логе видно, что именно ушло наружу: обещание из шапки скилла."""
    assert screen.describe((1600, 900), 350 * 1024) == "1600x900, 350 КБ"
