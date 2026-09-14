"""Панель управления: статус, модули, ключи, права и лог в одном окне.

Страницу отдаёт сам Jarvis на 127.0.0.1, окно открывает трей — Edge в режиме
приложения, без адресной строки. Новых зависимостей ноль: HTTP свой
(`http.py`), страница — один файл (`static/index.html`).

**Кто может сюда ходить.** Порт на 127.0.0.1 открыт любой странице в браузере
и любой программе на машине, а панель умеет менять ключи и пересобирать exe.
Поэтому:

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
import hmac
import logging
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.core.contracts import Event
from jarvis.core.version import current

from .http import HttpServer, Request, Response, json_response
from .settings import is_admin, key_list, launcher_level, set_disabled, tail, update_env

if TYPE_CHECKING:
    from jarvis.core.bus import EventBus
    from jarvis.core.config import JarvisConfig
    from jarvis.core.llm import LLMService
    from jarvis.core.skills import SkillManager
    from jarvis.core.tools import ToolRegistry

logger = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"

STARTING, READY, LISTENING, SPEAKING, STOPPING = "starting", "ready", "listening", "speaking", "stopping"

#: Политика содержимого страницы: всё своё, скрипты и стили — только встроенные.
_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
)


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
        sink: Any = None,
        port: int | None = None,
    ) -> None:
        self._config = config
        self._events = events
        self._registry = registry
        self._skills = skills
        self._llm = llm
        self._sink = sink
        self._token = secrets.token_urlsafe(24)
        self._server = HttpServer(self._handle, port=config.gui.port if port is None else port)
        self._started = time.time()
        self._state = STARTING
        self._heard = ""
        self._reply = ""
        self._outputs: dict[Any, str] = {}

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

    async def stop(self) -> None:
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
            ("GET", "/api/keys"): self._keys,
            ("POST", "/api/keys"): self._set_key,
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
        except ValueError as exc:
            return json_response({"error": str(exc)}, status=400)

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
            try:
                from jarvis.core.audio.outputs import query_outputs

                listed = await asyncio.to_thread(query_outputs, self._config.audio.output_names)
            except Exception:  # noqa: BLE001 — нет звука — нет и названия
                listed = []
            self._outputs = {item.index: item.spoken for item in listed}
        return self._outputs.get(device, str(device))

    async def _modules(self, request: Request) -> Response:
        health = await self._skills.health()
        loaded = set(self._skills.loaded)
        rows = []
        for candidate in self._skills.candidates():
            instance = self._skills.get(candidate.name)
            status = health.get(candidate.name)
            rows.append({
                "name": candidate.name,
                "parent": candidate.parent,
                "loaded": candidate.name in loaded,
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
        source = self._config.source
        text = await asyncio.to_thread(source.read_text, encoding="utf-8")
        await asyncio.to_thread(_write_atomic, source, set_disabled(text, disabled))
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

    async def _log(self, request: Request) -> Response:
        from jarvis.core.tray.session import current_log_file

        path = current_log_file()
        if path is None:
            return json_response({"text": "", "offset": 0})
        offset = int(request.query.get("offset", "-1") or -1)
        text, offset = await asyncio.to_thread(tail, path, offset)
        return json_response({"text": text, "offset": offset})


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
