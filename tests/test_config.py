"""Конфигурация: подстановка переменных окружения, проверки, пути."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.core.config import load_config
from jarvis.core.errors import ConfigError

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
