"""Панель управления: кто может в неё ходить и что она меняет.

Порт на 127.0.0.1 открыт любой странице в браузере, а панель пишет в `.env` и
в config.yaml. Поэтому первым делом проверяется отказ: без токена, с чужим
заголовком Host. Дальше — что правки доходят до файлов и до живой системы.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jarvis.core.bus import LocalEventBus
from jarvis.core.contracts import (
    AssistantSpeaking,
    SystemStarted,
    VoiceCommandRecognized,
    WakeWordDetected,
)
from jarvis.core.gui import panel as panel_module
from jarvis.core.gui.http import Request
from jarvis.core.gui.panel import ControlPanel
from jarvis.core.llm.service import Spending
from jarvis.core.skills.base import HealthStatus

CONFIG_YAML = "skills:\n  paths:\n    - skills\n  disabled: []        # не грузить\nrouter: {}\n"


class FakeSkills:
    def __init__(self) -> None:
        self.loaded = ("keys", "windows")
        self.disabled: frozenset[str] = frozenset()
        self.unloaded: list[str] = []
        self.adopted: list[str] = []

    def candidates(self) -> list[Any]:
        return [SimpleNamespace(name=name, parent="") for name in ("keys", "windows")]

    def get(self, name: str) -> Any:
        return SimpleNamespace(meta=SimpleNamespace(version="0.1.0", description=f"скилл {name}"))

    def tools_of(self, name: str) -> tuple[str, ...]:
        return (f"{name}.one",)

    async def health(self) -> dict[str, HealthStatus]:
        return {"keys": HealthStatus.healthy()}

    def set_disabled(self, names: Any) -> None:
        self.disabled = frozenset(names)

    async def unload(self, name: str) -> None:
        self.unloaded.append(name)

    async def adopt(self, name: str) -> None:
        self.adopted.append(name)


def _panel(tmp_path: Path) -> tuple[ControlPanel, FakeSkills, LocalEventBus]:
    source = tmp_path / "config.yaml"
    source.write_text(CONFIG_YAML, encoding="utf-8")
    config = SimpleNamespace(
        root=tmp_path,
        source=source,
        app=SimpleNamespace(name="Jarvis"),
        gui=SimpleNamespace(port=0),
        skills=SimpleNamespace(paths=(tmp_path / "skills",)),
        audio=SimpleNamespace(output_names={}),
        logging=SimpleNamespace(level="INFO"),
    )
    profiles = SimpleNamespace(
        tasks=lambda: ("intent",),
        get=lambda task: SimpleNamespace(provider="openai", model="gpt-5.4-nano"),
    )
    events = LocalEventBus()
    skills = FakeSkills()
    panel = ControlPanel(
        config=config,  # type: ignore[arg-type]
        events=events,
        registry=[1, 2, 3],  # type: ignore[arg-type]
        skills=skills,  # type: ignore[arg-type]
        llm=SimpleNamespace(spending=Spending(), profiles=profiles),  # type: ignore[arg-type]
    )
    return panel, skills, events


def _request(panel: ControlPanel, method: str, path: str, *, body: Any = None, token: str | None = None,
             host: str | None = None, query: dict[str, str] | None = None) -> Request:
    headers = {"host": host or f"127.0.0.1:{panel._server.port}"}
    if token is not None:
        headers["x-jarvis-token"] = token
    raw = json.dumps(body).encode() if body is not None else b""
    return Request(method, path, query or {}, headers, raw)


def _json(response: Any) -> Any:
    return json.loads(response.body.decode("utf-8"))


# --- доступ -----------------------------------------------------------------


async def test_api_refuses_without_token(tmp_path: Path) -> None:
    panel, _, _ = _panel(tmp_path)
    assert (await panel._handle(_request(panel, "GET", "/api/status"))).status == 403
    assert (await panel._handle(_request(panel, "GET", "/api/status", token="guess"))).status == 403


async def test_foreign_host_is_refused_even_with_token(tmp_path: Path) -> None:
    """Подмена DNS: чужой домен указывает на 127.0.0.1, но Host у запроса чужой."""
    panel, _, _ = _panel(tmp_path)
    response = await panel._handle(_request(panel, "GET", "/api/status", token=panel.token, host="evil.example"))
    assert response.status == 403


async def test_page_needs_token_and_forbids_framing(tmp_path: Path) -> None:
    panel, _, _ = _panel(tmp_path)
    assert (await panel._handle(_request(panel, "GET", "/"))).status == 403
    page = await panel._handle(_request(panel, "GET", "/", query={"token": panel.token}))
    assert page.status == 200 and b"J.A.R.V.I.S." in page.body
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]


# --- статус -----------------------------------------------------------------


async def test_status_follows_the_assistant(tmp_path: Path) -> None:
    panel, _, events = _panel(tmp_path)
    await panel.start()
    try:
        async def state() -> Any:
            return _json(await panel._handle(_request(panel, "GET", "/api/status", token=panel.token)))

        assert (await state())["state"] == "starting"
        await events.publish(SystemStarted(source="app"))
        await events.publish(WakeWordDetected(source="voice"))
        assert (await state())["state"] == "listening"
        await events.publish(VoiceCommandRecognized(source="voice", text="как дела"))
        await events.publish(AssistantSpeaking(source="voice", text="Всё работает."))
        status = await state()
    finally:
        await panel.stop()
    assert status["state"] == "speaking"
    assert (status["last_heard"], status["last_reply"]) == ("как дела", "Всё работает.")
    assert status["skills"] == 2 and status["tools"] == 3
    assert status["profiles"] == [{"task": "intent", "model": "openai/gpt-5.4-nano"}]


# --- модули -----------------------------------------------------------------


async def test_disabling_module_writes_config_and_unloads(tmp_path: Path) -> None:
    panel, skills, _ = _panel(tmp_path)
    response = await panel._handle(
        _request(panel, "POST", "/api/modules", token=panel.token, body={"name": "keys", "enabled": False})
    )
    assert response.status == 200
    assert "  disabled: [keys]        # не грузить" in (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert skills.unloaded == ["keys"] and skills.disabled == {"keys"}

    await panel._handle(_request(panel, "POST", "/api/modules", token=panel.token, body={"name": "keys", "enabled": True}))
    assert "  disabled: []" in (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert skills.adopted == ["keys"]


async def test_unknown_module_is_a_clear_refusal(tmp_path: Path) -> None:
    panel, _, _ = _panel(tmp_path)
    response = await panel._handle(
        _request(panel, "POST", "/api/modules", token=panel.token, body={"name": "../evil", "enabled": False})
    )
    assert response.status == 400


async def test_modules_list_versions_and_health(tmp_path: Path) -> None:
    panel, _, _ = _panel(tmp_path)
    rows = _json(await panel._handle(_request(panel, "GET", "/api/modules", token=panel.token)))["modules"]
    keys = next(row for row in rows if row["name"] == "keys")
    assert keys["loaded"] and keys["healthy"] and keys["tools"] == 1 and keys["version"] == "0.1.0"


# --- ключи ------------------------------------------------------------------


async def test_key_is_saved_and_never_returned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_TEST_KEY", raising=False)
    (tmp_path / ".env").write_text("# мои ключи\nOTHER=1\n", encoding="utf-8")
    panel, _, _ = _panel(tmp_path)

    saved = await panel._handle(
        _request(panel, "POST", "/api/keys", token=panel.token, body={"name": "JARVIS_TEST_KEY", "value": "sk-very-secret"})
    )
    listed = await panel._handle(_request(panel, "GET", "/api/keys", token=panel.token))

    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "JARVIS_TEST_KEY=sk-very-secret" in env and env.startswith("# мои ключи\n")
    assert os.environ["JARVIS_TEST_KEY"] == "sk-very-secret"
    assert b"sk-very-secret" not in saved.body and b"sk-very-secret" not in listed.body
    key = next(item for item in _json(listed)["keys"] if item["name"] == "JARVIS_TEST_KEY")
    assert key["present"] and key["length"] == len("sk-very-secret")


# --- права и лог ------------------------------------------------------------


async def test_admin_reads_launcher_manifest(tmp_path: Path) -> None:
    (tmp_path / "Jarvis.exe").write_bytes(b'MZ...level="asInvoker"...')
    panel, _, _ = _panel(tmp_path)
    info = _json(await panel._handle(_request(panel, "GET", "/api/admin", token=panel.token)))
    assert info["launcher"] == "asInvoker"
    assert isinstance(info["admin"], bool)


async def test_log_is_followed_by_offset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "jarvis.log"
    log.write_bytes("строка\n".encode())
    import jarvis.core.tray.session as tray_session

    monkeypatch.setattr(tray_session, "current_log_file", lambda: log)
    panel, _, _ = _panel(tmp_path)
    first = _json(await panel._handle(_request(panel, "GET", "/api/log", token=panel.token, query={"offset": "-1"})))
    assert first["text"] == "строка\n"
    again = _json(await panel._handle(
        _request(panel, "GET", "/api/log", token=panel.token, query={"offset": str(first["offset"])})
    ))
    assert again["text"] == ""


def test_panel_module_does_not_log_the_token() -> None:
    source = Path(panel_module.__file__).read_text(encoding="utf-8")
    assert "self.url" not in source.split("def start", 1)[1].split("def stop", 1)[0]
