"""Панель управления: правка настроек и разбор HTTP.

Панель пишет в файлы владельца, полные его же комментариев, поэтому главное
здесь — что меняется ровно одна строка, а ключ не утекает даже частично.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from jarvis.core.gui.http import (
    HttpServer,
    Request,
    Response,
    encode_response,
    json_response,
    parse_head,
)
from jarvis.core.gui.settings import (
    describe_config,
    key_list,
    launcher_level,
    read_env,
    referenced_keys,
    set_disabled,
    set_scalar,
    set_top_value,
    tail,
    update_env,
    valid_time,
)

ENV = """# ключи
JARVIS_OPENAI_KEY=sk-secret-value
JARVIS_FISH_KEY=
CLI_CLAUDE='quoted'
"""


# --- ключи ------------------------------------------------------------------


def test_env_is_read_like_the_config_loader() -> None:
    assert read_env(ENV) == {"JARVIS_OPENAI_KEY": "sk-secret-value", "JARVIS_FISH_KEY": "", "CLI_CLAUDE": "quoted"}


def test_key_list_never_contains_values() -> None:
    sources = {"config/config.yaml": "api_key: ${JARVIS_OPENAI_KEY:-}\nkey: ${JARVIS_DEEPGRAM_KEY}"}
    keys = {item.name: item for item in key_list(ENV, sources)}

    assert keys["JARVIS_OPENAI_KEY"].present and keys["JARVIS_OPENAI_KEY"].length == len("sk-secret-value")
    assert keys["JARVIS_OPENAI_KEY"].used_by == ("config/config.yaml",)
    # Пустое значение — ключа нет.
    assert not keys["JARVIS_FISH_KEY"].present
    # Ссылается конфиг, а в .env нет — тоже показываем: именно такой и нужно задать.
    assert not keys["JARVIS_DEEPGRAM_KEY"].present
    assert "sk-secret-value" not in repr(list(keys.values()))


def test_references_are_collected_per_file() -> None:
    found = referenced_keys({"a.yaml": "${X} ${Y:-1}", "b.yaml": "${X}"})
    assert found == {"X": ("a.yaml", "b.yaml"), "Y": ("a.yaml",)}


def test_examples_in_comments_are_not_keys() -> None:
    """Панель показывала ключ VAR «не задан»: это пример `${VAR}` из комментария."""
    found = referenced_keys({"a.yaml": "# те же ${VAR} работают и тут\nkey: ${REAL}  # ${ALSO_NOT}"})
    assert found == {"REAL": ("a.yaml",)}


def test_update_env_replaces_one_line_and_keeps_comments() -> None:
    updated = update_env(ENV, "JARVIS_OPENAI_KEY", "sk-new")
    assert "JARVIS_OPENAI_KEY=sk-new" in updated
    assert "sk-secret-value" not in updated
    assert updated.startswith("# ключи\n")
    assert "CLI_CLAUDE='quoted'" in updated


def test_update_env_appends_and_removes() -> None:
    added = update_env(ENV, "JARVIS_DEEPGRAM_KEY", "dg")
    assert added.rstrip().endswith("JARVIS_DEEPGRAM_KEY=dg")
    removed = update_env(ENV, "JARVIS_OPENAI_KEY", "")
    assert "JARVIS_OPENAI_KEY" not in removed


@pytest.mark.parametrize(("name", "value"), [("BAD NAME", "x"), ("OK", "a\nEVIL=1")])
def test_update_env_refuses_injection(name: str, value: str) -> None:
    with pytest.raises(ValueError):
        update_env(ENV, name, value)


# --- модули -----------------------------------------------------------------

CONFIG = """attention:
  enabled: true
skills:
  paths:
    - skills          # положил файл сюда
  disabled: []        # имена скиллов, которые не грузить
  settings: {}
router:
  disabled: [something]
"""


def test_set_disabled_rewrites_only_the_skills_line() -> None:
    updated = set_disabled(CONFIG, ["telegram", "keys"])
    assert "  disabled: [keys, telegram]        # имена скиллов, которые не грузить" in updated
    # Такая же строка в чужой секции не тронута.
    assert "  disabled: [something]" in updated
    assert updated.replace("[keys, telegram]", "[]") == CONFIG


SETTINGS = """audio:
  engine: sounddevice
  input_device: null       # null = по умолчанию
  vad:
    engine: silero
persona:
  address: сэр
attention:
  quiet_from: "23:30"
"""


def test_set_scalar_changes_one_value_and_keeps_the_comment() -> None:
    updated = set_scalar(SETTINGS, "audio", "input_device", "Микрофон (Audio Device), MME")
    assert '  input_device: "Микрофон (Audio Device), MME"       # null = по умолчанию' in updated
    back = set_scalar(updated, "audio", "input_device", None)
    assert back == SETTINGS


def test_set_scalar_touches_only_first_level_keys() -> None:
    """`audio.engine` и `audio.vad.engine` называются одинаково."""
    updated = set_scalar(SETTINGS, "audio", "engine", "null")
    assert '  engine: "null"' in updated and "    engine: silero" in updated


def test_set_scalar_quotes_and_refuses_missing() -> None:
    assert '  quiet_from: "22:00"' in set_scalar(SETTINGS, "attention", "quiet_from", "22:00")
    assert '  address: "босс"' in set_scalar(SETTINGS, "persona", "address", "босс")
    with pytest.raises(ValueError):
        set_scalar(SETTINGS, "persona", "name", "x")


SKILL_CONFIG = """# Настройки скилла «keys». Живут рядом с кодом.

# Включён ли наблюдатель.
# Выключен по умолчанию.
enabled: false   # true — следить
url: https://panel
api_key: ${CLI_CLAUDE:-}
timeout: 15
# Окна, где не следим.
skip:
  - bitwarden
  - банк
reactions:
  # комментарий внутри
  "не работает": ["Как всегда, сэр."]
review: true
"""


def test_config_becomes_form_fields_with_comment_hints() -> None:
    fields = {item.key: item for item in describe_config(SKILL_CONFIG)}
    assert list(fields) == ["enabled", "url", "api_key", "timeout", "skip", "reactions", "review"]
    assert fields["enabled"].kind == "bool" and fields["enabled"].help == "Включён ли наблюдатель. Выключен по умолчанию."
    assert fields["timeout"].kind == "int" and fields["url"].kind == "str"
    assert fields["skip"].kind == "list" and fields["skip"].value == ["bitwarden", "банк"]
    assert fields["reactions"].kind == "dict"
    # Ссылка на .env — только для чтения, иначе форма затёрла бы её значением.
    assert fields["api_key"].env and not fields["url"].env


def test_commented_example_is_not_the_hint_of_the_next_key() -> None:
    """Живой случай: подсказка к `extension` начиналась с «рутрекер: https://…»."""
    text = (
        "# Свои поисковики: слева — как называешь.\n"
        "engines: {}\n"
        '#   рутрекер: "https://rutracker.org/forum/tracker.php?nm={query}"\n'
        "# Расширение браузера: работает вкладками.\n"
        "extension:\n  enabled: true\n"
    )
    fields = {item.key: item for item in describe_config(text)}
    assert fields["extension"].help == "Расширение браузера: работает вкладками."
    assert fields["engines"].help == "Свои поисковики: слева — как называешь."


def test_simple_value_changes_in_place_and_keeps_the_comment() -> None:
    updated = set_top_value(SKILL_CONFIG, "enabled", True)
    assert "enabled: true   # true — следить" in updated
    assert updated.replace("enabled: true", "enabled: false") == SKILL_CONFIG


def test_list_is_rewritten_as_a_block_and_the_rest_is_untouched() -> None:
    updated = set_top_value(SKILL_CONFIG, "skip", ["keepass", "пароль"])
    assert "skip:\n- keepass\n- пароль\nreactions:" in updated
    assert updated.startswith(SKILL_CONFIG.split("skip:")[0])
    assert updated.endswith('review: true\n')
    assert yaml.safe_load(updated)["reactions"] == {"не работает": ["Как всегда, сэр."]}


def test_unknown_key_is_refused() -> None:
    with pytest.raises(ValueError):
        set_top_value(SKILL_CONFIG, "nope", 1)


def test_quiet_time_format() -> None:
    assert valid_time("23:30") and valid_time("") and not valid_time("24:00") and not valid_time("7:30")


def test_set_disabled_refuses_strange_names_and_missing_line() -> None:
    with pytest.raises(ValueError):
        set_disabled(CONFIG, ["../evil"])
    with pytest.raises(ValueError):
        set_disabled("skills:\n  paths: []\n", ["keys"])


# --- права и лог ------------------------------------------------------------


def test_launcher_level_is_read_from_the_manifest() -> None:
    assert launcher_level(b"...requestedExecutionLevel level=\"requireAdministrator\"...") == "requireAdministrator"
    assert launcher_level(b"level=\"asInvoker\"") == "asInvoker"
    assert launcher_level(b"nothing") is None


def test_tail_follows_the_file(tmp_path: Path) -> None:
    # Байтами: на Windows write_text превратил бы \n в \r\n.
    log = tmp_path / "jarvis.log"
    log.write_bytes("первая\n".encode())
    text, offset = tail(log, -1)
    assert text == "первая\n"
    with log.open("ab") as file:
        file.write("вторая\n".encode())
    more, offset = tail(log, offset)
    assert more == "вторая\n"
    assert tail(log, offset) == ("", offset)


def test_tail_starts_over_when_the_file_shrinks(tmp_path: Path) -> None:
    log = tmp_path / "jarvis.log"
    log.write_bytes("новый день\n".encode())
    text, _ = tail(log, 10_000)
    assert text == "новый день\n"


def test_tail_does_not_cut_a_letter_in_half(tmp_path: Path) -> None:
    log = tmp_path / "jarvis.log"
    log.write_bytes("ааааа\nбббб\n".encode())
    text, offset = tail(log, 0, chunk=15)
    assert text == "ааааа\n"
    assert "�" not in text


# --- HTTP -------------------------------------------------------------------


def test_head_is_parsed() -> None:
    method, path, query, headers = parse_head(
        b"GET /api/log?offset=12 HTTP/1.1\r\nHost: 127.0.0.1:8766\r\nX-Jarvis-Token: t\r\n\r\n"
    )
    assert (method, path, query) == ("GET", "/api/log", {"offset": "12"})
    assert headers["x-jarvis-token"] == "t"


def test_response_forbids_framing_and_caching() -> None:
    raw = encode_response(json_response({"ok": True}))
    assert raw.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"X-Frame-Options: DENY" in raw and b"Cache-Control: no-store" in raw
    assert raw.endswith(b'{"ok": true}')


async def test_server_answers_real_requests() -> None:
    seen: list[Request] = []

    async def handler(request: Request) -> Response:
        seen.append(request)
        return json_response({"body": request.json()})

    server = HttpServer(handler, port=0)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        body = '{"name": "keys"}'.encode()
        writer.write(
            b"POST /api/modules HTTP/1.1\r\nHost: x\r\nContent-Length: "
            + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        await writer.drain()
        answer = await reader.read()
        writer.close()
    finally:
        await server.stop()
    assert answer.endswith(b'{"body": {"name": "keys"}}')
    assert seen[0].method == "POST" and seen[0].path == "/api/modules"
