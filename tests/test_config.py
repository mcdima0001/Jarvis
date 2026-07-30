"""Конфигурация: подстановка переменных окружения, проверки, пути."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from jarvis.core.config import load_config
from jarvis.core.errors import ConfigError
from jarvis.core.logging import setup_logging

_MINIMAL = """
app:
  name: TestJarvis
logging:
  dir: logs
skills:
  paths:
    - skills
llm:
  default_task: dialog
  providers:
    openrouter:
      type: openrouter
      api_key: ${TEST_JARVIS_KEY:-}
      base_url: https://example.invalid/v1
  profiles:
    dialog:
      provider: openrouter
      model: some/model
memory:
  dir: memory
  documents: [profile]
  journals: [today]
"""


def _write(tmp_path: Path, text: str) -> Path:
    """Разложить конфиг во временном проекте."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_env_substitution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Секрет приходит из окружения, а не лежит в конфиге."""
    monkeypatch.setenv("TEST_JARVIS_KEY", "секретный-ключ")
    config = load_config(_write(tmp_path, _MINIMAL))

    assert config.llm.providers["openrouter"].api_key == "секретный-ключ"
    assert config.llm.providers["openrouter"].configured


def test_default_when_env_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Без переменной берётся значение по умолчанию, приложение стартует."""
    monkeypatch.delenv("TEST_JARVIS_KEY", raising=False)
    config = load_config(_write(tmp_path, _MINIMAL))

    assert config.llm.providers["openrouter"].api_key == ""
    assert not config.llm.providers["openrouter"].configured


def test_missing_env_without_default_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ссылка на незаданную переменную без значения по умолчанию — явная ошибка."""
    monkeypatch.delenv("TEST_JARVIS_ABSENT", raising=False)
    text = _MINIMAL.replace("${TEST_JARVIS_KEY:-}", "${TEST_JARVIS_ABSENT}")

    with pytest.raises(ConfigError, match="TEST_JARVIS_ABSENT"):
        load_config(_write(tmp_path, text))


def test_paths_resolved_against_project_root(tmp_path: Path) -> None:
    """Относительные пути превращаются в абсолютные — в коде путей нет."""
    config = load_config(_write(tmp_path, _MINIMAL))

    assert config.root == tmp_path.resolve()
    assert config.skills.paths[0] == tmp_path.resolve() / "skills"
    assert config.memory.dir == tmp_path.resolve() / "memory"
    assert config.logging.dir.is_absolute()


def test_log_time_is_written_by_default(tmp_path: Path) -> None:
    """У записи в логе есть время, даже если в конфиге о нём ни слова.

    Без времени лог годится только на «что-то сломалось»: почти всё, что по
    нему выясняют, — это порядок событий и сколько заняло.
    """
    config = load_config(_write(tmp_path, _MINIMAL))
    root = logging.getLogger()
    kept = list(root.handlers)
    try:
        assert setup_logging(config.logging).name == "jarvis"
        formatter = root.handlers[0].formatter
        assert formatter is not None
        line = formatter.format(
            logging.LogRecord("jarvis", logging.INFO, __file__, 1, "проверка", None, None)
        )
        assert re.match(
            r"^\d{2}\.\d{2}\.\d{2}, \d{2}:\d{2}:\d{2} INFO\s+jarvis\s+проверка$", line
        ), line
    finally:
        # Логирование глобальное: оставить за собой файл во временном каталоге
        # значит сломать соседние тесты через полчаса и в другом файле.
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in kept:
            root.addHandler(handler)


def test_own_time_format_respected(tmp_path: Path) -> None:
    """Формат времени задаётся конфигом: в коде его быть не должно."""
    text = _MINIMAL.replace("logging:\n  dir: logs", 'logging:\n  dir: logs\n  time_format: "%H:%M"')
    config = load_config(_write(tmp_path, text))

    assert config.logging.time_format == "%H:%M"


def test_profile_with_unknown_provider_rejected(tmp_path: Path) -> None:
    """Опечатка в имени провайдера ловится при старте, а не в рантайме."""
    text = _MINIMAL.replace("provider: openrouter\n      model", "provider: opnrouter\n      model")

    with pytest.raises(ConfigError, match="opnrouter"):
        load_config(_write(tmp_path, text))


def test_missing_config_file(tmp_path: Path) -> None:
    """Отсутствующий файл конфигурации — понятная ошибка."""
    with pytest.raises(ConfigError, match="не найден"):
        load_config(tmp_path / "config" / "нет.yaml")


def test_persona_defaults(tmp_path: Path) -> None:
    """Без секции persona ассистент всё равно вежлив и здоровается."""
    config = load_config(_write(tmp_path, _MINIMAL))

    assert config.persona.address == {}
    assert config.persona.greet_on_start
    assert config.persona.farewell_on_stop


def test_persona_address_as_single_string(tmp_path: Path) -> None:
    """«address: сэр» одной строкой — значит, так на любом языке."""
    text = _MINIMAL + "\npersona:\n  address: сэр\n  greet_on_start: false\n"
    config = load_config(_write(tmp_path, text))

    assert config.persona.address == {"*": "сэр"}
    assert not config.persona.greet_on_start


def test_persona_own_phrases(tmp_path: Path) -> None:
    """Свои реплики доезжают из конфига до наборов персоны."""
    text = _MINIMAL + (
        "\npersona:\n"
        "  address:\n"
        "    ru: командир\n"
        "  phrases:\n"
        "    listening:\n"
        "      ru:\n"
        "        - \"Чего изволите?\"\n"
    )
    config = load_config(_write(tmp_path, text))

    assert config.persona.address == {"ru": "командир"}
    assert config.persona.phrases["listening"]["ru"] == ("Чего изволите?",)


def test_persona_phrases_must_be_grouped_by_language(tmp_path: Path) -> None:
    """Список сразу под ситуацией — забытый язык, а не рабочая настройка."""
    text = _MINIMAL + "\npersona:\n  phrases:\n    listening:\n      - Ага\n"

    with pytest.raises(ConfigError, match="listening"):
        load_config(_write(tmp_path, text))


# --- логирование -------------------------------------------------------------


def test_file_is_more_verbose_than_the_console(tmp_path, capsys) -> None:
    """Уровня два: консоль читают глазами, файл — когда что-то пошло не так.

    Идея владельца, и она чинит целый класс промахов. Пока подробности писались
    через `info`, у них появлялось условие «писать, только если есть что
    показать» — чтобы не мешать в консоли. Ровно такое условие однажды обнулило
    диагностику: страница отвечала «ничего не вижу», условие не выполнялось, и
    четыре попытки подряд не оставили в логе ни строки.
    """
    import logging

    from jarvis.core.config import LoggingConfig
    from jarvis.core.logging import setup_logging

    log = setup_logging(
        LoggingConfig(dir=tmp_path, file="t.log", console=True, level="INFO", file_level="DEBUG")
    )
    log.info("видно везде")
    log.debug("подробность")
    logging.shutdown()

    written = (tmp_path / "t.log").read_text(encoding="utf-8")
    assert "подробность" in written
    assert "видно везде" in written
    assert "подробность" not in capsys.readouterr().err


def test_third_party_debug_never_reaches_the_log(tmp_path) -> None:
    """Подробный файл не должен состоять из чужой отладки.

    На DEBUG numba вываливает дизассемблер каждой компилируемой функции — сотни
    строк на фразу. Поэтому корень держится на WARNING, а уровень из конфига
    достаётся только логгерам приложения.
    """
    import logging

    from jarvis.core.config import LoggingConfig
    from jarvis.core.logging import setup_logging

    setup_logging(LoggingConfig(dir=tmp_path, file="t.log", console=False, file_level="DEBUG"))
    logging.getLogger("numba.core.ssa").debug("дизассемблер")
    logging.getLogger("numba.core.ssa").warning("а это важно")
    logging.shutdown()

    written = (tmp_path / "t.log").read_text(encoding="utf-8")
    assert "дизассемблер" not in written
    assert "а это важно" in written


def test_typo_in_the_level_does_not_break_the_start(tmp_path) -> None:
    """Опечатка в уровне не должна ронять запуск: логи важнее их настройки."""
    import logging

    from jarvis.core.config import LoggingConfig
    from jarvis.core.logging import setup_logging

    log = setup_logging(
        LoggingConfig(dir=tmp_path, file="t.log", console=False, level="ИНФО", file_level="ОТЛАДКА")
    )
    log.debug("всё равно пишем")
    logging.shutdown()

    assert "всё равно пишем" in (tmp_path / "t.log").read_text(encoding="utf-8")
