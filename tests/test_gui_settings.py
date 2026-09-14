"""Панель управления: правка настроек и разбор HTTP.

Панель пишет в файлы владельца, полные его же комментариев, поэтому главное
здесь — что меняется ровно одна строка, а ключ не утекает даже частично.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.core.gui.http import (
    HttpServer,
    Request,
    Response,
    encode_response,
    json_response,
    parse_head,
)
from jarvis.core.gui.settings import (
    key_list,
    launcher_level,
    read_env,
    referenced_keys,
    set_disabled,
    tail,
    update_env,
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
