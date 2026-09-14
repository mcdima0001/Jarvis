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
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(".")
        self.loaded = ("keys", "windows")
        self.disabled: frozenset[str] = frozenset()
        self.unloaded: list[str] = []
        self.adopted: list[str] = []
        self.settings: dict[str, Any] = {}

    def candidates(self) -> list[Any]:
        return [
            SimpleNamespace(name=name, parent="", path=self.root / "skills" / name / "skill.py")
            for name in ("keys", "windows")
        ]

    def set_settings(self, name: str, settings: Any) -> None:
        self.settings[name] = settings

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

    def has(self, name: str) -> bool:
        return name.startswith("author.")

    def catalog(self, *, skill: str | None = None) -> Any:
        spec = SimpleNamespace(
            name=f"{skill}.watch", description="Следить за клавиатурой.\n\nПодробности.",
            phrases=("следи за клавиатурой",), routable=False, reversible=True,
        )
        return SimpleNamespace(specs=(spec,))

    def declared(self, name: str) -> Any:
        return SimpleNamespace(routable=False, reversible=None)

    def overrides(self, name: str) -> dict[str, Any]:
        return {"reversible": True}

    def set_override(self, name: str, flag: str, value: bool | None) -> Any:
        if flag not in ("routable", "reversible"):
            raise ValueError("не правится")
        self.calls.append(("flag", {"tool": name, flag: value}))
        return SimpleNamespace(routable=False, reversible=value)

    async def invoke(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        self.calls.append((name, arguments))
        if name == "author.accept":
            return ToolResult.success(None, speech={"ru": "Скилл keys принят и подключён."})
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
    skills = FakeSkills(tmp_path)
    (tmp_path / "skills" / "keys").mkdir(parents=True)
    (tmp_path / "skills" / "keys" / "skill.py").write_text("# keys\n", encoding="utf-8")
    (tmp_path / "skills" / "keys" / "config.yaml").write_text("# настройки\nenabled: false\n", encoding="utf-8")
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


# --- карточка модуля --------------------------------------------------------


async def test_module_detail_shows_tools_and_config(tmp_path: Path) -> None:
    panel, _, _ = _panel(tmp_path)
    info = _json(await _call(panel, "GET", "/api/modules/detail", query={"name": "keys"}))
    assert info["tools"] == [{
        "name": "keys.watch", "description": "Следить за клавиатурой.",
        "phrases": ["следи за клавиатурой"], "routable": False, "reversible": True,
        "declared": {"routable": False, "reversible": None}, "overridden": ["reversible"],
    }]
    config = info["config"]
    assert {key: config[key] for key in ("editable", "exists", "path", "text")} == {
        "editable": True, "exists": True, "path": "skills/keys/config.yaml", "text": "# настройки\nenabled: false\n",
    }
    assert config["fields"][0]["key"] == "enabled" and config["fields"][0]["kind"] == "bool"
    assert info["improvable"] is True


async def test_tool_flag_is_changed_from_the_card(tmp_path: Path) -> None:
    panel, _, _ = _panel(tmp_path)
    registry = panel._registry
    response = await _call(panel, "POST", "/api/modules/flag", body={"tool": "keys.watch", "flag": "reversible", "value": None})
    assert response.status == 200
    assert _json(response)["reversible"] is None
    assert ("flag", {"tool": "keys.watch", "reversible": None}) in registry.calls
    # Не булево и не null — отказ, а не «истина по Python».
    bad = await _call(panel, "POST", "/api/modules/flag", body={"tool": "keys.watch", "flag": "reversible", "value": "да"})
    assert bad.status == 400
    wrong = await _call(panel, "POST", "/api/modules/flag", body={"tool": "keys.watch", "flag": "timeout", "value": True})
    assert wrong.status == 400


async def test_broken_yaml_is_refused_and_file_kept(tmp_path: Path) -> None:
    panel, skills, _ = _panel(tmp_path)
    response = await _call(panel, "POST", "/api/modules/config", body={"name": "keys", "text": "enabled: [true"})
    assert response.status == 400
    assert (tmp_path / "skills" / "keys" / "config.yaml").read_text(encoding="utf-8") == "# настройки\nenabled: false\n"
    assert skills.adopted == []


async def test_saved_config_is_applied_and_module_reloaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANEL_TEST_KEY", "секрет")
    panel, skills, _ = _panel(tmp_path)
    text = "# настройки\nenabled: true\nkey: ${PANEL_TEST_KEY}\n"
    response = await _call(panel, "POST", "/api/modules/config", body={"name": "keys", "text": text})
    assert response.status == 200
    assert (tmp_path / "skills" / "keys" / "config.yaml").read_text(encoding="utf-8") == text
    # ${VAR} раскрыт так же, как при запуске, а модуль сразу перезагружен.
    assert skills.settings["keys"] == {"enabled": True, "key": "секрет"}
    assert skills.adopted == ["keys"]


async def test_form_fields_come_from_config_with_hints(tmp_path: Path) -> None:
    panel, _, _ = _panel(tmp_path)
    (tmp_path / "skills" / "keys" / "config.yaml").write_text(
        "# Следить за клавиатурой.\nenabled: false\nkey: ${PANEL_X:-}\nreactions:\n  баг: [Фича.]\n", encoding="utf-8"
    )
    fields = _json(await _call(panel, "GET", "/api/modules/detail", query={"name": "keys"}))["config"]["fields"]
    by_key = {field["key"]: field for field in fields}
    assert by_key["enabled"]["kind"] == "bool" and by_key["enabled"]["help"] == "Следить за клавиатурой."
    assert by_key["key"]["env"] is True
    assert by_key["reactions"]["yaml"] == "баг:\n- Фича.\n"


async def test_form_save_changes_only_touched_fields(tmp_path: Path) -> None:
    original = "# Следить за клавиатурой.\nenabled: false   # да или нет\ntimeout: 15\nreactions:\n  баг: [Фича.]\n"
    panel, skills, _ = _panel(tmp_path)
    (tmp_path / "skills" / "keys" / "config.yaml").write_text(original, encoding="utf-8")
    response = await _call(panel, "POST", "/api/modules/config", body={
        "name": "keys", "values": {"enabled": True, "timeout": "30"}, "yaml": {"reactions": "баг: [Фича., Ну конечно.]"},
    })
    assert response.status == 200
    text = (tmp_path / "skills" / "keys" / "config.yaml").read_text(encoding="utf-8")
    assert text.startswith("# Следить за клавиатурой.\nenabled: true   # да или нет\ntimeout: 30\n")
    assert skills.settings["keys"]["reactions"] == {"баг": ["Фича.", "Ну конечно."]}
    assert skills.adopted == ["keys"]


async def test_form_refuses_env_and_wrong_types(tmp_path: Path) -> None:
    panel, _, _ = _panel(tmp_path)
    (tmp_path / "skills" / "keys" / "config.yaml").write_text("key: ${PANEL_X:-}\ntimeout: 15\n", encoding="utf-8")
    env = await _call(panel, "POST", "/api/modules/config", body={"name": "keys", "values": {"key": "sk-leak"}})
    assert env.status == 400
    typed = await _call(panel, "POST", "/api/modules/config", body={"name": "keys", "values": {"timeout": "много"}})
    assert typed.status == 400
    assert (tmp_path / "skills" / "keys" / "config.yaml").read_text(encoding="utf-8") == "key: ${PANEL_X:-}\ntimeout: 15\n"


async def test_improvement_goes_to_author(tmp_path: Path) -> None:
    panel, _, _ = _panel(tmp_path)
    empty = await _call(panel, "POST", "/api/modules/improve", body={"name": "keys", "request": "  "})
    assert empty.status == 400
    response = await _call(panel, "POST", "/api/modules/improve", body={"name": "keys", "request": "не реагируй на пароли"})
    assert response.status == 200
    assert panel._registry.calls == [  # type: ignore[attr-defined]
        ("author.improve", {"skill": "keys", "request": "не реагируй на пароли"})
    ]


async def test_drafts_show_diff_and_review_and_accept_through_author(tmp_path: Path) -> None:
    draft = tmp_path / "drafts" / "keys"
    draft.mkdir(parents=True)
    (draft / "skill.py").write_text("# keys\n# доработано\n", encoding="utf-8")
    (draft / "review.md").write_text("проверь паузу\n", encoding="utf-8")
    panel, _, _ = _panel(tmp_path)

    drafts = _json(await _call(panel, "GET", "/api/drafts"))["drafts"]
    assert drafts[0]["name"] == "keys" and drafts[0]["improvement"] is True
    assert "+# доработано" in drafts[0]["diff"] and drafts[0]["review"] == "проверь паузу"

    accepted = await _call(panel, "POST", "/api/drafts/accept", body={"name": "keys"})
    assert _json(accepted)["message"] == "Скилл keys принят и подключён."


# --- положение окна ---------------------------------------------------------


def test_window_position_is_remembered_in_pixels(tmp_path: Path) -> None:
    panel, _, _ = _panel(tmp_path)
    assert panel.saved_window() is None
    assert panel.remember_window((-1500, 40, 1773, 894))
    assert panel.saved_window() == (-1500, 40, 1773, 894)
    # Мусор не запоминается: свёрнутое окно Windows уносит в −32000.
    assert not panel.remember_window((-32000, -32000, 160, 28))
    assert panel.saved_window() == (-1500, 40, 1773, 894)


def test_old_page_point_file_is_not_used(tmp_path: Path) -> None:
    """Первая версия писала точки страницы: такие координаты поставили бы окно не туда."""
    panel, _, _ = _panel(tmp_path)
    (tmp_path / "memory").mkdir(exist_ok=True)
    (tmp_path / "memory" / "panel_window.json").write_text('{"x": 10, "y": 10, "width": 1419, "height": 715}')
    assert panel.saved_window() is None


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


async def test_draft_is_sent_back_with_revise(tmp_path: Path) -> None:
    panel, _, _ = _panel(tmp_path)
    response = await _call(panel, "POST", "/api/drafts/revise", body={"name": "keys", "request": "короче"})
    assert response.status == 200
    assert panel._registry.calls[-1] == (  # type: ignore[attr-defined]
        "author.improve", {"skill": "keys", "request": "короче", "revise": True}
    )


async def test_usage_tab_shows_today_with_price(tmp_path: Path) -> None:
    """Просьба владельца: расход за сегодня с примерной ценой, отдельной вкладкой."""
    from jarvis.core.config.schema import ModelPrice
    from jarvis.core.llm.usage import UsageLog

    panel, _, _ = _panel(tmp_path)
    off = _json(await _call(panel, "GET", "/api/usage"))
    assert off["enabled"] is False and off["profiles"][0]["task"] == "intent"

    usage = UsageLog(tmp_path / "usage", prices={"gpt-5.4-nano": ModelPrice(input=0.2, cached=0.02, output=1.25)})
    usage.add("intent", "gpt-5.4-nano", {"prompt_tokens": 1000, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 900}})
    usage.add("place", "gpt-5.5", {"prompt_tokens": 500, "completion_tokens": 50})
    panel._llm.usage = usage  # type: ignore[attr-defined]

    data = _json(await _call(panel, "GET", "/api/usage"))
    rows = {row["task"]: row for row in data["today"]["rows"]}
    assert rows["intent"]["cached"] == 900 and rows["intent"]["cost"] > 0
    assert rows["place"]["cost"] is None
    assert data["today"]["total"]["unpriced"] == ["gpt-5.5"]
    assert len(data["history"]) == 7
    status = _json(await _call(panel, "GET", "/api/status"))
    assert status["today"]["tokens"] == 1560


def test_token_survives_restart_and_broken_file_is_replaced(tmp_path: Path) -> None:
    """Живой случай 14.09.2026: после перезапуска открытое окно писало «нет связи».

    Токен был новым на каждый запуск, и окно, открытое прошлым, получало отказ.
    """
    from jarvis.core.gui.panel import load_token

    first, _, _ = _panel(tmp_path)
    again, _, _ = _panel_again(tmp_path)
    assert first.token == again.token and len(first.token) >= 24
    token_file = tmp_path / "memory" / "panel_token"
    token_file.write_text("  ", encoding="utf-8")
    fresh = load_token(token_file)
    assert fresh != first.token and token_file.read_text(encoding="utf-8").strip() == fresh


def _panel_again(tmp_path: Path) -> tuple[ControlPanel, FakeSkills, LocalEventBus]:
    """Вторая панель на той же папке — как после перезапуска Jarvis."""
    import shutil

    shutil.rmtree(tmp_path / "skills", ignore_errors=True)
    return _panel(tmp_path)
