"""Панель управления: кто может в неё ходить и что она меняет.

Порт на 127.0.0.1 открыт любой странице в браузере, а панель пишет в `.env`,
в config.yaml и стирает записи памяти. Поэтому первым делом проверяется отказ:
без токена, с чужим заголовком Host. Дальше — что правки доходят до файлов и до
живой системы.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jarvis.core.audio import outputs as audio_devices
from jarvis.core.bus import LocalEventBus
from jarvis.core.contracts import (
    AssistantSpeaking,
    SystemStarted,
    ToolResult,
    VoiceCommandRecognized,
    WakeWordDetected,
)
from jarvis.core.gui import panel as panel_module
from jarvis.core.gui.http import Request
from jarvis.core.gui.panel import ControlPanel
from jarvis.core.llm.service import Spending
from jarvis.core.memory.protocol import JournalEntry
from jarvis.core.skills.base import HealthStatus

CONFIG_YAML = """attention:
  enabled: true
  quiet_from: "23:30"
  quiet_to: "08:30"
skills:
  paths:
    - skills
  disabled: []        # не грузить
audio:
  engine: sounddevice
  input_device: null       # null = по умолчанию
persona:
  address: сэр
router: {}
"""


class FakeSkills:
    def __init__(self) -> None:
        self.loaded = ("keys", "windows")
        self.disabled: frozenset[str] = frozenset()
        self.unloaded: list[str] = []
        self.adopted: list[str] = []

    def candidates(self) -> list[Any]:
        return [SimpleNamespace(name=name, parent="") for name in ("keys", "windows")]

    def get(self, name: str) -> Any:
        return SimpleNamespace(meta=SimpleNamespace(name=name, version="0.1.0", description=f"скилл {name}"))

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


class FakeRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __len__(self) -> int:
        return 3

    async def invoke(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        self.calls.append((name, arguments))
        return ToolResult.success(None, speech={"ru": "Голос переключён: колонка."})


class FakeDocuments:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {"preferences": {"кофе": "без сахара", "музыка": ["рок"]}, "studio": {}}

    def namespaces(self) -> tuple[str, ...]:
        return tuple(self.data)

    async def read(self, namespace: str) -> dict[str, Any]:
        return dict(self.data[namespace])

    async def write(self, namespace: str, data: Any) -> None:
        self.data[namespace] = dict(data)


class FakeJournals:
    def namespaces(self) -> tuple[str, ...]:
        return ("today",)

    async def recent(self, namespace: str, *, limit: int = 20) -> list[JournalEntry]:
        return [JournalEntry(timestamp=1.0, text="старое"), JournalEntry(timestamp=2.0, text="новое", tags=("погода",))]


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
        registry=FakeRegistry(),  # type: ignore[arg-type]
        skills=skills,  # type: ignore[arg-type]
        llm=SimpleNamespace(spending=Spending(), profiles=profiles),  # type: ignore[arg-type]
        memory=SimpleNamespace(documents=FakeDocuments(), journals=FakeJournals()),  # type: ignore[arg-type]
    )
    return panel, skills, events


def _request(panel: ControlPanel, method: str, path: str, *, body: Any = None, token: str | None = None,
             host: str | None = None, query: dict[str, str] | None = None) -> Request:
    headers = {"host": host or f"127.0.0.1:{panel._server.port}"}
    if token is not None:
        headers["x-jarvis-token"] = token
    raw = json.dumps(body).encode() if body is not None else b""
    return Request(method, path, query or {}, headers, raw)


async def _call(panel: ControlPanel, method: str, path: str, **kwargs: Any) -> Any:
    return await panel._handle(_request(panel, method, path, token=panel.token, **kwargs))


def _json(response: Any) -> Any:
    return json.loads(response.body.decode("utf-8"))


# --- доступ -----------------------------------------------------------------


async def test_api_refuses_without_token(tmp_path: Path) -> None:
    panel, _, _ = _panel(tmp_path)
    assert (await panel._handle(_request(panel, "GET", "/api/status"))).status == 403
    assert (await panel._handle(_request(panel, "GET", "/api/memory", token="guess"))).status == 403


async def test_foreign_host_is_refused_even_with_token(tmp_path: Path) -> None:
    """Подмена DNS: чужой домен указывает на 127.0.0.1, но Host у запроса чужой."""
    panel, _, _ = _panel(tmp_path)
    response = await panel._handle(_request(panel, "GET", "/api/status", token=panel.token, host="evil.example"))
    assert response.status == 403


async def test_window_icons_are_large_pngs(tmp_path: Path) -> None:
    """16 точек из ICO Edge растягивал в мыло: окну отдаются крупные PNG."""
    panel, _, _ = _panel(tmp_path)
    icon = await panel._handle(_request(panel, "GET", "/icon-256.png"))
    assert icon.status == 200 and icon.content_type == "image/png"
    assert icon.body[:8] == b"\x89PNG\r\n\x1a\n"
    assert (await panel._handle(_request(panel, "GET", "/icon-7.png"))).status == 404


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
        assert _json(await _call(panel, "GET", "/api/status"))["state"] == "starting"
        await events.publish(SystemStarted(source="app"))
        await events.publish(WakeWordDetected(source="voice"))
        assert _json(await _call(panel, "GET", "/api/status"))["state"] == "listening"
        await events.publish(VoiceCommandRecognized(source="voice", text="как дела"))
        await events.publish(AssistantSpeaking(source="voice", text="Всё работает."))
        status = _json(await _call(panel, "GET", "/api/status"))
    finally:
        await panel.stop()
    assert status["state"] == "speaking"
    assert (status["last_heard"], status["last_reply"]) == ("как дела", "Всё работает.")
    assert status["skills"] == 2 and status["tools"] == 3
    assert status["profiles"] == [{"task": "intent", "model": "openai/gpt-5.4-nano"}]


# --- модули -----------------------------------------------------------------


async def test_disabling_module_writes_config_and_unloads(tmp_path: Path) -> None:
    panel, skills, _ = _panel(tmp_path)
    response = await _call(panel, "POST", "/api/modules", body={"name": "keys", "enabled": False})
    assert response.status == 200
    assert "  disabled: [keys]        # не грузить" in (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert skills.unloaded == ["keys"] and skills.disabled == {"keys"}

    await _call(panel, "POST", "/api/modules", body={"name": "keys", "enabled": True})
    assert "  disabled: []" in (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert skills.adopted == ["keys"]


async def test_unknown_module_is_a_clear_refusal(tmp_path: Path) -> None:
    panel, _, _ = _panel(tmp_path)
    response = await _call(panel, "POST", "/api/modules", body={"name": "../evil", "enabled": False})
    assert response.status == 400


async def test_modules_list_versions_and_health(tmp_path: Path) -> None:
    panel, _, _ = _panel(tmp_path)
    rows = _json(await _call(panel, "GET", "/api/modules"))["modules"]
    keys = next(row for row in rows if row["name"] == "keys")
    assert keys["loaded"] and keys["healthy"] and keys["tools"] == 1 and keys["version"] == "0.1.0"


# --- память -----------------------------------------------------------------


async def test_memory_shows_documents_and_newest_journal_first(tmp_path: Path) -> None:
    panel, _, _ = _panel(tmp_path)
    data = _json(await _call(panel, "GET", "/api/memory"))
    preferences = next(doc for doc in data["documents"] if doc["namespace"] == "preferences")
    assert {"key": "кофе", "value": "без сахара"} in preferences["items"]
    assert {"key": "музыка", "value": '["рок"]'} in preferences["items"]
    today = data["journals"][0]
    assert today["name"] == "today" and today["entries"][0]["text"] == "новое"
    assert today["entries"][0]["tags"] == ["погода"]


async def test_forget_removes_one_memory_record(tmp_path: Path) -> None:
    panel, _, _ = _panel(tmp_path)
    response = await _call(panel, "POST", "/api/memory/forget", body={"namespace": "preferences", "key": "кофе"})
    assert response.status == 200
    documents = panel._memory.documents  # type: ignore[union-attr]
    assert documents.data["preferences"] == {"музыка": ["рок"]}  # type: ignore[attr-defined]
    missing = await _call(panel, "POST", "/api/memory/forget", body={"namespace": "нет", "key": "кофе"})
    assert missing.status == 400


# --- настройки --------------------------------------------------------------

MIC = audio_devices.Output(index=1, name="Микрофон (Audio Device)", spoken="Микрофон Audio Device",
                           raw="Микрофон (Audio Device)", hostapi="MME")


@pytest.fixture
def devices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio_devices, "query_inputs", lambda: [MIC])
    monkeypatch.setattr(audio_devices, "query_outputs", lambda names=None: [])


async def test_settings_show_config_values(tmp_path: Path, devices: None) -> None:
    panel, _, _ = _panel(tmp_path)
    data = _json(await _call(panel, "GET", "/api/settings"))
    assert data["address"] == "сэр"
    assert (data["quiet_from"], data["quiet_to"]) == ("23:30", "08:30")
    assert data["input"]["options"] == [{"name": "Микрофон (Audio Device)", "value": "Микрофон (Audio Device), MME"}]
    assert data["extension"]["connected"] is False


async def test_microphone_is_written_with_its_interface(tmp_path: Path, devices: None) -> None:
    panel, _, _ = _panel(tmp_path)
    response = await _call(panel, "POST", "/api/settings", body={"field": "input", "value": "Микрофон (Audio Device), MME"})
    assert response.status == 200
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert '  input_device: "Микрофон (Audio Device), MME"       # null = по умолчанию' in text
    # Чего нет в списке, того не пишем: строка уедет в sounddevice.
    refused = await _call(panel, "POST", "/api/settings", body={"field": "input", "value": "evil"})
    assert refused.status == 400


async def test_output_goes_through_the_voice_command(tmp_path: Path) -> None:
    panel, _, _ = _panel(tmp_path)
    response = await _call(panel, "POST", "/api/settings", body={"field": "output", "value": "Динамики (JBL Flip 6)"})
    assert _json(response)["message"] == "Голос переключён: колонка."
    assert panel._registry.calls == [("core.set_output", {"device": "Динамики (JBL Flip 6)"})]  # type: ignore[attr-defined]


async def test_address_and_quiet_hours_are_validated(tmp_path: Path) -> None:
    panel, _, _ = _panel(tmp_path)
    await _call(panel, "POST", "/api/settings", body={"field": "address", "value": "босс"})
    await _call(panel, "POST", "/api/settings", body={"field": "quiet_from", "value": "22:00"})
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert '  address: "босс"' in text and '  quiet_from: "22:00"' in text
    bad = await _call(panel, "POST", "/api/settings", body={"field": "quiet_to", "value": "25:99"})
    assert bad.status == 400


# --- ключи ------------------------------------------------------------------


async def test_key_is_saved_and_never_returned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_TEST_KEY", raising=False)
    (tmp_path / ".env").write_text("# мои ключи\nOTHER=1\n", encoding="utf-8")
    panel, _, _ = _panel(tmp_path)

    saved = await _call(panel, "POST", "/api/keys", body={"name": "JARVIS_TEST_KEY", "value": "sk-very-secret"})
    listed = await _call(panel, "GET", "/api/keys")

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
    info = _json(await _call(panel, "GET", "/api/admin"))
    assert info["launcher"] == "asInvoker"
    assert isinstance(info["admin"], bool)


async def test_log_is_followed_by_offset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "jarvis.log"
    log.write_bytes("строка\n".encode())
    import jarvis.core.tray.session as tray_session

    monkeypatch.setattr(tray_session, "current_log_file", lambda: log)
    panel, _, _ = _panel(tmp_path)
    first = _json(await _call(panel, "GET", "/api/log", query={"offset": "-1"}))
    assert first["text"] == "строка\n"
    again = _json(await _call(panel, "GET", "/api/log", query={"offset": str(first["offset"])}))
    assert again["text"] == ""


def test_panel_module_does_not_log_the_token() -> None:
    source = Path(panel_module.__file__).read_text(encoding="utf-8")
    assert "self.url" not in source.split("def start", 1)[1].split("def stop", 1)[0]
