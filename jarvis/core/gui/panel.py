"""Панель управления: статус, модули, память, права, лог и настройки в одном окне.

Страницу отдаёт сам Jarvis на 127.0.0.1, окно открывает трей — Edge в режиме
приложения, без адресной строки. Новых зависимостей ноль: HTTP свой
(`http.py`), страница — один файл (`static/index.html`).

**Кто может сюда ходить.** Порт на 127.0.0.1 открыт любой странице в браузере
и любой программе на машине, а панель умеет менять ключи, стирать память и
пересобирать exe. Поэтому:

* **токен** на каждый запуск, случайный; знает его только трей, который
  открывает окно. Страница получает его в адресе и дальше шлёт заголовком.
  Чужая страница такой заголовок на наш порт отправить не может: для этого
  нужен ответ на предварительный запрос CORS, а мы его не даём;
* **заголовок Host** обязан быть нашим адресом — защита от подмены DNS, при
  которой чужой домен начинает указывать на 127.0.0.1;
* страница не встраивается в чужие рамки и не кешируется.

**Ключи не отдаются никогда**, даже частично: только задан ли и какой длины.
"""

from __future__ import annotations

import asyncio
import difflib
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from jarvis.core.audio import outputs as audio_devices
from jarvis.core.config import load_skill_settings
from jarvis.core.contracts import Event
from jarvis.core.errors import ConfigError, SkillError
from jarvis.core.logging.visible import console_view
from jarvis.core.version import current

from .http import HttpServer, Request, Response, json_response
from .settings import (
    is_admin,
    key_list,
    launcher_level,
    set_disabled,
    set_scalar,
    tail,
    update_env,
    valid_time,
)

if TYPE_CHECKING:
    from jarvis.core.bus import EventBus
    from jarvis.core.config import JarvisConfig
    from jarvis.core.llm import LLMService
    from jarvis.core.memory import Memory
    from jarvis.core.skills import SkillManager
    from jarvis.core.tools import ToolRegistry

logger = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"
#: Значки трея: окно панели носит тот же.
ICONS = Path(__file__).resolve().parent.parent / "tray"
ICON_SIZES = (32, 48, 64, 128, 256)

STARTING, READY, LISTENING, SPEAKING, STOPPING = "starting", "ready", "listening", "speaking", "stopping"

#: Политика содержимого страницы: всё своё, скрипты и стили — только встроенные.
_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
)

#: Сколько знаков значения из памяти показывать: там бывают целые словари сайтов.
PREVIEW = 400
#: Сколько записей журнала показывать.
JOURNAL_LIMIT = 50


class ControlPanel:
    """Сервис панели: поднимает HTTP и следит за состоянием ассистента по шине."""

    def __init__(
        self,
        *,
        config: JarvisConfig,
        events: EventBus,
        registry: ToolRegistry,
        skills: SkillManager,
        llm: LLMService,
        memory: Memory | None = None,
        sink: Any = None,
        port: int | None = None,
    ) -> None:
        self._config = config
        self._events = events
        self._registry = registry
        self._skills = skills
        self._llm = llm
        self._memory = memory
        self._sink = sink
        self._token = secrets.token_urlsafe(24)
        self._server = HttpServer(self._handle, port=config.gui.port if port is None else port)
        self._started = time.time()
        self._state = STARTING
        self._heard = ""
        self._reply = ""
        self._outputs: dict[Any, str] = {}
        #: Наблюдение за положением окна панели — только на Windows.
        self._window_task: asyncio.Task[None] | None = None

    @property
    def service_name(self) -> str:
        return "gui"

    @property
    def url(self) -> str:
        """Адрес окна панели вместе с токеном. В лог не пишется."""
        return f"http://127.0.0.1:{self._server.port}/?token={self._token}"

    @property
    def token(self) -> str:
        return self._token

    async def start(self) -> None:
        for name, handler in (
            ("system.started", self._on_ready),
            ("voice.wake_word.detected", self._on_listening),
            ("voice.command.recognized", self._on_heard),
            ("input.command.typed", self._on_heard),
            ("assistant.speaking", self._on_speaking),
            ("assistant.replied", self._on_replied),
            ("system.stopping", self._on_stopping),
        ):
            self._events.subscribe(name, handler)
        try:
            await self._server.start()
        except OSError as exc:
            # Порт занят (второй Jarvis, чужая программа) — ассистент без панели
            # лучше, чем не запустившийся ассистент.
            logger.warning("Панель управления не поднялась на порту %d: %s", self._server.port, exc)
            return
        logger.info("Панель управления: http://127.0.0.1:%d (окно — из трея)", self._server.port)
        if sys.platform == "win32":
            self._window_task = asyncio.create_task(self._watch_window(), name="panel-window")

    async def stop(self) -> None:
        if self._window_task is not None:
            self._window_task.cancel()
            self._window_task = None
        await self._server.stop()

    # --- состояние по шине ---------------------------------------------------

    async def _on_ready(self, event: Event) -> None:
        self._state = READY

    async def _on_listening(self, event: Event) -> None:
        self._state = LISTENING

    async def _on_heard(self, event: Event) -> None:
        self._heard = str(getattr(event, "text", ""))

    async def _on_speaking(self, event: Event) -> None:
        self._state = SPEAKING
        self._reply = str(getattr(event, "text", ""))

    async def _on_replied(self, event: Event) -> None:
        if self._state != STOPPING:
            self._state = READY

    async def _on_stopping(self, event: Event) -> None:
        self._state = STOPPING

    # --- запросы -------------------------------------------------------------

    async def _handle(self, request: Request) -> Response:
        port = self._server.port
        if request.headers.get("host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return Response(status=403, body="Чужой адрес".encode())

        icon = self._icon_path(request.path)
        if icon is not None:
            # Значок окна и кнопки на панели задач — тот же реактор, что в трее.
            # Без токена: секрета в нём нет, а браузер просит его сам.
            data = await asyncio.to_thread(_read_bytes_or_empty, icon)
            if not data:
                return Response(status=404)
            kind = "image/png" if icon.suffix == ".png" else "image/x-icon"
            return Response(body=data, content_type=kind)

        if request.path == "/":
            if not hmac.compare_digest(request.query.get("token", ""), self._token):
                return Response(status=403, body="Открой панель из трея Jarvis.".encode())
            page = await asyncio.to_thread((STATIC / "index.html").read_bytes)
            return Response(
                body=page,
                content_type="text/html; charset=utf-8",
                headers={"Content-Security-Policy": _CSP, "Referrer-Policy": "no-referrer"},
            )

        if not request.path.startswith("/api/"):
            return Response(status=404)
        if not hmac.compare_digest(request.headers.get("x-jarvis-token", ""), self._token):
            return json_response({"error": "нет доступа"}, status=403)

        routes = {
            ("GET", "/api/status"): self._status,
            ("GET", "/api/modules"): self._modules,
            ("POST", "/api/modules"): self._toggle_module,
            ("POST", "/api/modules/reload"): self._reload_module,
            ("GET", "/api/modules/detail"): self._module_detail,
            ("POST", "/api/modules/config"): self._save_module_config,
            ("POST", "/api/modules/improve"): self._improve_module,
            ("GET", "/api/drafts"): self._drafts,
            ("POST", "/api/drafts/accept"): self._accept_draft,
            ("POST", "/api/drafts/discard"): self._discard_draft,
            ("GET", "/api/memory"): self._memory_view,
            ("POST", "/api/memory/forget"): self._forget,
            ("GET", "/api/settings"): self._settings,
            ("POST", "/api/settings"): self._change_setting,
            ("GET", "/api/keys"): self._keys,
            ("POST", "/api/keys"): self._set_key,
            ("POST", "/api/extension/folder"): self._extension_folder,
            ("GET", "/api/admin"): self._admin,
            ("POST", "/api/admin"): self._build_launcher,
            ("GET", "/api/log"): self._log,
        }
        route = routes.get((request.method, request.path))
        if route is None:
            known = any(path == request.path for _, path in routes)
            return json_response({"error": "нет такого запроса"}, status=405 if known else 404)
        try:
            return await route(request)
        except (ValueError, SkillError, ConfigError) as exc:
            # Отказ, понятный человеку, а не «сервер упал»: модуль не загрузился,
            # уже загружен, выключен — это ответ, а не авария панели.
            return json_response({"error": str(exc)}, status=400)

    @staticmethod
    def _icon_path(path: str) -> Path | None:
        if path == "/favicon.ico":
            return ICONS / "jarvis.ico"
        match = re.fullmatch(r"/icon-(\d+)\.png", path)
        if match and int(match.group(1)) in ICON_SIZES:
            return ICONS / f"jarvis-{match.group(1)}.png"
        return None

    # --- статус --------------------------------------------------------------

    async def _status(self, request: Request) -> Response:
        build = current()
        spending = self._llm.spending
        profiles = []
        for task in self._llm.profiles.tasks():
            profile = self._llm.profiles.get(task)
            profiles.append({"task": task, "model": f"{profile.provider}/{profile.model}"})
        return json_response({
            "name": self._config.app.name,
            "version": build.version,
            "commit": build.commit,
            "state": self._state,
            "last_heard": self._heard,
            "last_reply": self._reply,
            "started": time.strftime("%H:%M", time.localtime(self._started)),
            "uptime_s": int(time.time() - self._started),
            "skills": len(self._skills.loaded),
            "tools": len(self._registry),
            "admin": await asyncio.to_thread(is_admin),
            "output": await self._output_label(),
            "spending": {
                "calls": spending.calls,
                "tokens": spending.total_tokens,
                "by_task": dict(spending.by_task),
            },
            "profiles": profiles,
        })

    async def _output_label(self) -> str:
        """Куда сейчас говорит голос. Список устройств спрашивается один раз."""
        device = getattr(self._sink, "device", None)
        if device is None:
            return ""
        if device not in self._outputs:
            listed = await self._query(audio_devices.query_outputs, self._config.audio.output_names)
            self._outputs = {item.index: item.spoken for item in listed}
        return self._outputs.get(device, str(device))

    @staticmethod
    async def _query(function: Any, *args: Any) -> list[audio_devices.Output]:
        try:
            return list(await asyncio.to_thread(function, *args))
        except Exception:  # noqa: BLE001 — нет звука — нет и списка
            return []

    # --- модули --------------------------------------------------------------

    async def _modules(self, request: Request) -> Response:
        health = await self._skills.health()
        loaded = set(self._skills.loaded)
        rows = []
        for candidate in self._skills.candidates():
            # Панель и конфиг знают папку, записи — имя из паспорта; они могут
            # различаться (`powershell` назвал себя `clipboard`).
            instance = self._skills.get(candidate.name)
            real = instance.meta.name if instance else candidate.name
            status = health.get(real)
            rows.append({
                "name": candidate.name,
                "parent": candidate.parent,
                "loaded": real in loaded,
                "disabled": candidate.name in self._skills.disabled,
                "version": instance.meta.version if instance else "",
                "description": instance.meta.description if instance else "",
                "tools": len(self._skills.tools_of(candidate.name)),
                "healthy": status.ok if status else False,
                "detail": status.detail if status else "",
            })
        return json_response({"modules": rows})

    async def _toggle_module(self, request: Request) -> Response:
        data = request.json()
        name, enabled = str(data.get("name", "")), bool(data.get("enabled"))
        candidates = {item.name: item for item in self._skills.candidates()}
        if name not in candidates:
            raise ValueError(f"нет модуля {name!r}")
        children = [item.name for item in candidates.values() if item.parent == name]

        disabled = set(self._skills.disabled)
        if enabled:
            disabled.discard(name)
        else:
            disabled.add(name)
        await self._edit_config(lambda text: set_disabled(text, disabled))
        self._skills.set_disabled(disabled)

        if enabled:
            await self._skills.adopt(name)
            for child in children:
                if child not in disabled:
                    await self._skills.adopt(child)
            message = f"Модуль {name} включён."
        else:
            for child in children:
                await self._skills.unload(child)
            await self._skills.unload(name)
            message = f"Модуль {name} выключен и не загрузится после перезапуска."
        logger.info("Панель: %s", message)
        return json_response({"message": message})

    async def _reload_module(self, request: Request) -> Response:
        name = str(request.json().get("name", ""))
        await self._skills.adopt(name)
        logger.info("Панель: модуль %s перезагружен", name)
        return json_response({"message": f"Модуль {name} перезагружен."})

    # --- карточка модуля: команды, настройки, доработка ---------------------

    def _candidate(self, name: str) -> Any:
        for item in self._skills.candidates():
            if item.name == name:
                return item
        raise ValueError(f"нет модуля {name!r}")

    @staticmethod
    def _config_file(candidate: Any) -> Path | None:
        """Где лежат настройки модуля: `config.yaml` рядом с его `skill.py`.

        У скилла одним файлом (`skills/demo.py`) своей папки нет — и настроек
        рядом с ним не бывает.
        """
        path = Path(candidate.path)
        return path.parent / "config.yaml" if path.name == "skill.py" else None

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self._config.root.resolve()).as_posix()
        except ValueError:
            return str(path)

    async def _module_detail(self, request: Request) -> Response:
        name = request.query.get("name", "")
        candidate = self._candidate(name)
        instance = self._skills.get(name)
        meta = instance.meta if instance else None
        specs = self._registry.catalog(skill=meta.name).specs if meta else ()
        config = self._config_file(candidate)
        text = await asyncio.to_thread(_read_or_empty, config) if config else ""
        return json_response({
            "name": name,
            "passport": meta.name if meta else "",
            "version": meta.version if meta else "",
            "description": meta.description if meta else "",
            "loaded": instance is not None,
            "parent": candidate.parent,
            "tools": [
                {
                    "name": spec.name,
                    "description": (spec.description or "").strip().splitlines()[0] if spec.description else "",
                    "phrases": list(spec.phrases),
                    "routable": spec.routable,
                    "reversible": spec.reversible,
                }
                for spec in specs
            ],
            "config": {
                "editable": config is not None,
                "exists": bool(config and config.is_file()),
                "path": self._relative(config) if config else "",
                "text": text,
            },
            # Подскилл дорабатывать нельзя: принятый черновик лёг бы не в ту папку.
            "improvable": not candidate.parent and self._registry.has("author.improve"),
        })

    async def _save_module_config(self, request: Request) -> Response:
        data = request.json()
        name, text = str(data.get("name", "")), str(data.get("text", ""))
        config = self._config_file(self._candidate(name))
        if config is None:
            raise ValueError("у этого модуля нет своей папки — настроек рядом с ним не бывает")
        try:
            parsed = yaml.safe_load(text) if text.strip() else {}
        except yaml.YAMLError as exc:
            raise ValueError(f"YAML не разобрался: {exc}") from exc
        if parsed is not None and not isinstance(parsed, dict):
            raise ValueError("настройки должны быть словарём «ключ: значение»")

        await asyncio.to_thread(_write_atomic, config, text if text.endswith("\n") else text + "\n")
        overrides = ((await self._config_data()).get("skills") or {}).get("settings") or {}
        settings = await asyncio.to_thread(load_skill_settings, config, overrides.get(name) or {})
        instance = self._skills.get(name)
        self._skills.set_settings(instance.meta.name if instance else name, settings)
        logger.info("Панель: настройки модуля %s сохранены", name)
        if instance is None:
            return json_response({"message": f"Настройки {name} сохранены. Модуль не загружен — применятся при включении."})
        await self._skills.adopt(name)
        return json_response({"message": f"Настройки {name} сохранены, модуль перезагружен."})

    async def _improve_module(self, request: Request) -> Response:
        data = request.json()
        name, wish = str(data.get("name", "")), str(data.get("request", "")).strip()
        self._candidate(name)
        if not wish:
            raise ValueError("напиши, что доработать")
        if not self._registry.has("author.improve"):
            raise ValueError("модуль author не загружен — дорабатывать некому")
        result = await self._registry.invoke("author.improve", {"skill": name, "request": wish})
        if not result.ok:
            raise ValueError(result.speech_for("ru") or str(result.error))
        return json_response({
            "message": "Отправил Claude на доработку. Это займёт пару минут: черновик появится "
            "вверху вкладки «Модули», а я доложу голосом."
        })

    def _collect_drafts(self) -> list[dict[str, Any]]:
        root = self._config.root
        folder = root / "drafts"
        rows: list[dict[str, Any]] = []
        if not folder.is_dir():
            return rows
        for draft in sorted(folder.glob("*/skill.py")):
            name = draft.parent.name
            current = root / "skills" / name / "skill.py"
            before = _read_or_empty(current).splitlines()
            after = _read_or_empty(draft).splitlines()
            review = draft.parent / "review.md"
            rows.append({
                "name": name,
                "improvement": current.is_file(),
                "diff": "\n".join(difflib.unified_diff(
                    before, after, f"skills/{name}/skill.py", f"drafts/{name}/skill.py", lineterm=""
                )),
                "review": _read_or_empty(review).strip() if review.is_file() else "",
            })
        return rows

    async def _drafts(self, request: Request) -> Response:
        return json_response({"drafts": await asyncio.to_thread(self._collect_drafts)})

    async def _draft_action(self, request: Request, tool_name: str) -> Response:
        name = str(request.json().get("name", ""))
        if not self._registry.has(tool_name):
            raise ValueError("модуль author не загружен")
        result = await self._registry.invoke(tool_name, {"name": name})
        message = result.speech_for("ru") or str(result.error or "")
        if not result.ok:
            raise ValueError(message)
        return json_response({"message": message})

    async def _accept_draft(self, request: Request) -> Response:
        return await self._draft_action(request, "author.accept")

    async def _discard_draft(self, request: Request) -> Response:
        return await self._draft_action(request, "author.discard")

    # --- память --------------------------------------------------------------

    def _require_memory(self) -> Memory:
        if self._memory is None:
            raise ValueError("память не подключена")
        return self._memory

    async def _memory_view(self, request: Request) -> Response:
        memory = self._require_memory()
        documents = []
        for namespace in memory.documents.namespaces():
            data = await memory.documents.read(namespace)
            documents.append({
                "namespace": namespace,
                "items": [{"key": key, "value": _preview(value)} for key, value in sorted(data.items())],
            })
        journals = []
        for namespace in memory.journals.namespaces():
            entries = await memory.journals.recent(namespace, limit=JOURNAL_LIMIT)
            journals.append({
                "name": namespace,
                "entries": [
                    {
                        "time": time.strftime("%d.%m %H:%M", time.localtime(entry.timestamp)),
                        "text": entry.text,
                        "tags": list(entry.tags),
                    }
                    for entry in reversed(entries)
                ],
            })
        return json_response({"documents": documents, "journals": journals})

    async def _forget(self, request: Request) -> Response:
        memory = self._require_memory()
        data = request.json()
        namespace, key = str(data.get("namespace", "")), str(data.get("key", ""))
        if namespace not in memory.documents.namespaces():
            raise ValueError(f"нет раздела памяти {namespace!r}")
        values = await memory.documents.read(namespace)
        if key not in values:
            raise ValueError(f"в разделе {namespace} нет записи {key!r}")
        del values[key]
        await memory.documents.write(namespace, values)
        logger.info("Панель: из памяти %s удалена запись", namespace)
        return json_response({"message": f"Запись удалена из раздела {namespace}."})

    # --- настройки -----------------------------------------------------------

    async def _config_data(self) -> dict[str, Any]:
        text = await asyncio.to_thread(_read_or_empty, self._config.source)
        data = yaml.safe_load(text) or {}
        return data if isinstance(data, dict) else {}

    async def _edit_config(self, change: Any) -> None:
        source = self._config.source
        text = await asyncio.to_thread(source.read_text, encoding="utf-8")
        await asyncio.to_thread(_write_atomic, source, change(text))

    async def _settings(self, request: Request) -> Response:
        data = await self._config_data()
        audio = data.get("audio") or {}
        persona = data.get("persona") or {}
        attention = data.get("attention") or {}
        outputs = await self._query(audio_devices.query_outputs, self._config.audio.output_names)
        inputs = await self._query(audio_devices.query_inputs)
        device = getattr(self._sink, "device", None)
        return json_response({
            "output": {
                "current": next((item.name for item in outputs if item.index == device), ""),
                "options": [{"name": item.name, "spoken": item.spoken} for item in outputs],
            },
            "input": {
                "current": audio.get("input_device"),
                "options": [
                    {"name": item.name, "value": audio_devices.config_value(item)} for item in inputs
                ],
            },
            "address": persona.get("address") or "",
            "quiet_from": attention.get("quiet_from") or "",
            "quiet_to": attention.get("quiet_to") or "",
            "extension": await self._extension_info(),
        })

    async def _change_setting(self, request: Request) -> Response:
        data = request.json()
        field, value = str(data.get("field", "")), data.get("value")
        restart = " Заработает после перезапуска Jarvis."

        if field == "output":
            result = await self._registry.invoke(
                "core.set_output", {"device": str(value) if value else "по умолчанию"}
            )
            message = result.speech_for("ru") or ("Готово." if result.ok else str(result.error))
            if not result.ok:
                raise ValueError(message)
            return json_response({"message": message})

        if field == "input":
            if value not in (None, ""):
                options = {
                    audio_devices.config_value(item)
                    for item in await self._query(audio_devices.query_inputs)
                }
                if value not in options:
                    raise ValueError("такого микрофона нет в списке")
            chosen = value or None
            await self._edit_config(lambda text: set_scalar(text, "audio", "input_device", chosen))
            return json_response({"message": "Микрофон выбран." + restart})

        if field == "address":
            address = str(value or "").strip()
            if len(address) > 40 or "\n" in address:
                raise ValueError("обращение — одно-два слова")
            await self._edit_config(lambda text: set_scalar(text, "persona", "address", address))
            return json_response({"message": "Обращение сохранено." + restart})

        if field in ("quiet_from", "quiet_to"):
            moment = str(value or "").strip()
            if not valid_time(moment):
                raise ValueError("время пишется как 23:30")
            await self._edit_config(lambda text: set_scalar(text, "attention", field, moment))
            return json_response({"message": "Время тишины сохранено." + restart})

        raise ValueError(f"нет такой настройки {field!r}")

    async def _extension_info(self) -> dict[str, Any]:
        folder = self._config.root / "extension"
        detail, connected = "модуль browser не загружен", False
        instance = self._skills.get("browser")
        health = getattr(instance, "health", None)
        if health is not None:
            try:
                status = await health()
                detail = status.detail
                connected = "расширение подключено" in detail
            except Exception as exc:  # noqa: BLE001 — сломанная проверка не гасит страницу
                detail = f"проверка не удалась: {exc}"
        return {
            "connected": connected,
            "detail": detail,
            "folder": str(folder),
            "token": (folder / "token.json").exists(),
        }

    async def _extension_folder(self, request: Request) -> Response:
        folder = self._config.root / "extension"
        if sys.platform == "win32":
            os.startfile(folder)  # noqa: S606 — путь наш
        return json_response({"message": f"Открыта папка {folder}"})

    # --- ключи ---------------------------------------------------------------

    def _config_sources(self) -> dict[str, str]:
        root = self._config.root
        files = [self._config.source]
        for path in self._config.skills.paths:
            files += sorted(Path(path).glob("*/config.yaml")) + sorted(Path(path).glob("*/*/config.yaml"))
        sources: dict[str, str] = {}
        for file in files:
            try:
                label = file.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                label = str(file)
            try:
                sources[label] = file.read_text(encoding="utf-8")
            except OSError:
                continue
        return sources

    async def _keys(self, request: Request) -> Response:
        env = self._config.root / ".env"
        text = await asyncio.to_thread(_read_or_empty, env)
        sources = await asyncio.to_thread(self._config_sources)
        keys = key_list(text, sources)
        return json_response({"keys": [
            {"name": key.name, "present": key.present, "length": key.length, "used_by": list(key.used_by)}
            for key in keys
        ]})

    async def _set_key(self, request: Request) -> Response:
        data = request.json()
        name, value = str(data.get("name", "")), str(data.get("value", ""))
        env = self._config.root / ".env"
        text = await asyncio.to_thread(_read_or_empty, env)
        await asyncio.to_thread(_write_atomic, env, update_env(text, name, value))
        if value.strip():
            os.environ[name] = value.strip()
            message = f"Ключ {name} сохранён. Заработает после перезапуска Jarvis."
        else:
            os.environ.pop(name, None)
            message = f"Ключ {name} убран из .env."
        # Значение в лог не пишется ни в каком виде.
        logger.info("Панель: ключ %s %s", name, "изменён" if value.strip() else "удалён")
        return json_response({"message": message})

    # --- права ---------------------------------------------------------------

    def _launcher(self) -> Path:
        return self._config.root / "Jarvis.exe"

    async def _admin(self, request: Request) -> Response:
        exe = self._launcher()
        data = await asyncio.to_thread(_read_bytes_or_empty, exe)
        return json_response({
            "admin": await asyncio.to_thread(is_admin),
            "launcher": launcher_level(data) if data else None,
            "launcher_path": str(exe),
        })

    async def _build_launcher(self, request: Request) -> Response:
        admin = bool(request.json().get("admin"))
        script = self._config.root / "launcher" / "build.py"
        command = [sys.executable, str(script)] + ([] if admin else ["--no-admin"])
        result = await asyncio.to_thread(
            subprocess.run, command, capture_output=True, timeout=120,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        output = (result.stdout or b"").decode("utf-8", "replace").strip()
        errors = (result.stderr or b"").decode("utf-8", "replace").strip()
        if result.returncode != 0:
            raise ValueError(f"сборка не удалась: {(errors or output)[-400:]}")
        logger.info("Панель: Jarvis.exe пересобран, права: %s", "администратор" if admin else "пользователь")
        return json_response({"message": (output.splitlines() or ["Собрано."])[-1] + " Действует со следующего запуска."})

    # --- окно ----------------------------------------------------------------

    def _window_file(self) -> Path:
        memory = getattr(self._config, "memory", None)
        base = getattr(memory, "dir", None) or self._config.root / "memory"
        return Path(base) / "panel_window.json"

    def saved_window(self) -> tuple[int, int, int, int] | None:
        """Где окно панели было в прошлый раз, в физических пикселях: x, y, ширина, высота.

        Файл без пометки `"units": "px"` — от первой версии, где положение
        сообщала страница в своих точках; такой не годится и не используется.
        """
        try:
            data = json.loads(self._window_file().read_text(encoding="utf-8"))
            if data.get("units") != "px":
                return None
            x, y, width, height = (int(data[key]) for key in ("x", "y", "width", "height"))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        return x, y, width, height

    def remember_window(self, geometry: tuple[int, int, int, int]) -> bool:
        """Запомнить положение окна. ``False`` — не похоже на настоящее окно."""
        x, y, width, height = geometry
        if not (320 <= width <= 20000 and 240 <= height <= 20000 and -30000 < x < 40000 and -30000 < y < 40000):
            return False
        path = self._window_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(path, json.dumps({"units": "px", "x": x, "y": y, "width": width, "height": height}))
        return True

    async def _watch_window(self) -> None:
        """Раз в две секунды спрашивать у Windows, где окно панели, и запоминать изменения.

        Спрашивается у Windows, а не у страницы: ставит окно на место тоже
        WinAPI (см. `window.py`), и единицы у обоих концов обязаны совпадать.
        """
        from . import window

        last = self.saved_window()
        while True:
            await asyncio.sleep(2.0)
            try:
                rect = await asyncio.to_thread(window.panel_rect)
                if rect is not None and rect != last and await asyncio.to_thread(self.remember_window, rect):
                    last = rect
            except Exception as exc:  # noqa: BLE001 — наблюдение за окном не гасит панель
                logger.debug("Панель: положение окна не прочитано: %s", exc)

    # --- лог -----------------------------------------------------------------

    async def _log(self, request: Request) -> Response:
        from jarvis.core.tray.session import current_log_file

        path = current_log_file()
        if path is None:
            return json_response({"text": "", "offset": 0})
        offset = int(request.query.get("offset", "-1") or -1)
        text, offset = await asyncio.to_thread(tail, path, offset)
        # Как в консоли: отладочные строки остаются в файле, а не в окне.
        text = console_view(text, self._config.logging.level)
        return json_response({"text": text, "offset": offset})


def _preview(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= PREVIEW else text[:PREVIEW] + "…"


def _read_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_bytes_or_empty(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _write_atomic(path: Path, text: str) -> None:
    """Записать через временный файл: оборванная запись не испортит `.env`."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
