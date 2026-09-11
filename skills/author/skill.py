"""Ассистент, который пишет себе новые умения.

Последний из четырёх недостающих органов. До него Jarvis умел ровно то, что мы
успели написать руками: новая возможность требовала человека с редактором.
Теперь «научись выключать монитор» превращается в скилл, а скилл — в инструмент,
который останется навсегда.

**Работу делает панель CloudCLI**, а не языковая модель из `llm.profiles`. Разница
существенная: там агент-кодер, который умеет читать, писать и проверять файлы, и
на написание скилла у него уходит около минуты. Обычная модель выдала бы текст
одним куском и без проверки.

**Минута — это и есть причина, по которой всё идёт фоновым поручением.** Стоять
с открытым микрофоном столько бессмысленно; ассистент отвечает «займусь» и
докладывает, когда готово.

**Готовый скилл сам собой не включается, и это главное решение здесь.** Он
ложится в `drafts/`, а не в `skills/`, — то есть автозагрузка его не видит даже
после перезапуска. Владелец читает файл и говорит «прими скилл такой-то»; только
тогда он переезжает в `skills/` и подключается на ходу. Код, написанный в облаке,
не должен начинать работать на живой машине оттого, что кто-то не глядя сказал
«да».

Почему не пулл-реквест, как задумывалось сперва. Он лучше: обзор в вебе, история,
откат. Но для него нужен токен GitHub, которого на 11.09.2026 нет ни в `.env`, ни
у пользователя панели, — а черновик работает уже сегодня и даёт ту же защиту:
человек смотрит раньше, чем код исполняется. Появится токен — путь через ветку
добавится рядом, не ломая этот.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import httpx

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool

#: Куда складывать написанное до одобрения. Намеренно **не** `skills/`:
#: автозагрузка смотрит туда, и черновик подключился бы сам при перезапуске.
DRAFTS = "drafts"

#: Сколько ждать панель. Минута — обычный срок, но агент иногда перепроверяет
#: себя, поэтому запас кратный.
TIMEOUT = 600.0

#: Ограждение от выдумок в имени: оно становится именем каталога.
NAME = re.compile(r"^[a-z][a-z0-9_]{1,30}$")

#: Имя скилла в его же паспорте — по нему и узнаём, как назвать каталог.
_META_NAME = re.compile(r"""name\s*=\s*["']([a-z][a-z0-9_]*)["']""")

#: Ограждение из тройных кавычек вокруг кода, если модель его всё же поставила.
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n\s*```\s*$", re.DOTALL)

#: Как устроен скилл в этом проекте. Идёт в запрос целиком: агент работает в
#: пустом каталоге и нашего кода не видит, а угадывать соглашения не должен.
CONVENTIONS = """Ты пишешь скилл для голосового ассистента Jarvis. Отвечай
ТОЛЬКО содержимым файла skill.py, без пояснений и без ограждений из обратных
кавычек. Первая строка ответа — строка кода.

Устройство скилла:

    \"\"\"Однострочное описание скилла по-русски.\"\"\"

    from __future__ import annotations

    from jarvis.core.contracts import ToolResult
    from jarvis.core.skills import HealthStatus, Skill, SkillMeta
    from jarvis.core.tools import tool


    class ИмяSkill(Skill):
        \"\"\"Что делает скилл.\"\"\"

        meta = SkillMeta(
            name="короткое_имя_латиницей",
            description="Что умеет, одной строкой.",
            version="0.1.0",
            spoken=("как_зовут_вслух", "spoken_name"),
        )

        @tool(phrases=["как это просят вслух"], reversible=True)
        async def что_делает(self, аргумент: str = "") -> ToolResult:
            \"\"\"Первая строка докстринга — описание для языковой модели.

            :param аргумент: что это такое.
            \"\"\"
            return ToolResult.success(
                {"полезная": "нагрузка"},
                speech={"ru": "Ответ вслух.", "en": "Spoken answer."},
            )

Обязательные правила:

* докстринги и комментарии по-русски, идентификаторы английские;
* блокирующие вызовы только через `asyncio.to_thread`, иначе встанет весь
  голосовой круг;
* `speech` — короткое предложение, его произносят вслух; списков и разметки
  быть не может;
* `reversible=True` у всего, что можно отменить; `reversible=False` у
  необратимого (отправить, удалить, закрыть с потерей работы);
* только стандартная библиотека и то, что уже есть в проекте; новых
  зависимостей не добавлять;
* имя в `meta.name` — латиницей, строчными, без пробелов.

Код проверяется линтером ruff и проверкой типов mypy, и пройти надо обе. Они
умеют тянуть в разные стороны, поэтому вот готовые ответы на уже пойманные
случаи:

* к Windows API обращаться через `ctypes.WinDLL("kernel32")`, а не через
  `ctypes.windll.kernel32`: второго нет в стабах на Linux, а обход через
  `getattr(ctypes, "windll")` не нравится линтеру;
* у `HealthStatus` есть только `healthy()` и `degraded()`, никаких `unhealthy`;
* платформозависимое сначала проверять через `platform.system()` или
  `sys.platform`, а недоступное отдавать как `ToolResult.failure` с понятной
  репликой, а не исключением."""


# --- чистые функции ---------------------------------------------------------


def build_prompt(what: str) -> str:
    """Собрать запрос к агенту-кодеру."""
    return f"{CONVENTIONS}\n\nЗадача: {what.strip()}"


def extract_code(text: str) -> str:
    """Достать код из ответа.

    Просьбу не ставить ограждения модель слышит не всегда, а лишние обратные
    кавычки превращают файл в синтаксическую ошибку. Дешевле снять их здесь,
    чем полагаться на послушание.
    """
    clean = text.strip()
    fenced = _FENCE.match(clean)
    return (fenced.group(1) if fenced else clean).strip() + "\n"


def skill_name(code: str) -> str:
    """Как скилл назвал сам себя. Пусто — не нашли."""
    found = _META_NAME.search(code)
    return found.group(1) if found else ""


def safe_name(name: str) -> str:
    """Имя, которым не страшно назвать каталог. Пусто — не годится.

    Проверка строгая и белым списком: имя приходит из текста, написанного
    моделью, а превращается в путь на диске. Чёрный список тут негоден в
    принципе — перечислить все способы выйти из каталога нельзя.
    """
    clean = name.strip().lower()
    return clean if NAME.match(clean) else ""


def draft_path(root: Path, name: str) -> Path:
    """Куда положить черновик. Всегда внутри `drafts/`, без исключений."""
    safe = safe_name(name)
    if not safe:
        raise ValueError(f"негодное имя скилла: {name!r}")
    return root / DRAFTS / safe / "skill.py"


def looks_like_skill(code: str) -> str:
    """Похоже ли написанное на скилл. Возвращает причину отказа или пусто.

    Проверка грубая и намеренно такая: разбирать чужой код по-настоящему —
    отдельная работа, а поймать «модель ответила извинением вместо файла» надо
    обязательно, иначе в черновиках окажется вежливый текст.
    """
    if not code.strip():
        return "пустой ответ"
    if "class " not in code or "Skill" not in code:
        return "в ответе нет класса скилла"
    if "@tool" not in code:
        return "в ответе нет ни одного инструмента"
    if not skill_name(code):
        return "скилл не назвал себя в meta"
    return ""


def repair_prompt(code: str, findings: str) -> str:
    """Попросить исправить найденное, прислав файл целиком."""
    return (
        f"{CONVENTIONS}\n\n"
        f"Вот скилл, который ты написал:\n\n{code}\n\n"
        f"Проверки нашли в нём вот что:\n\n{findings}\n\n"
        f"Исправь и пришли файл целиком. Имя в meta не меняй."
    )


def report(name: str, path: Path, tools: int, findings: str = "") -> str:
    """Что доложить, когда скилл написан.

    Про непройденные проверки говорится **первым делом**: доклад звучит один
    раз, и «готово» про код с ошибками — худший вид вранья, потому что он
    выглядит как успех.
    """
    verdict = "проверки не прошёл, смотри сам" if findings else "проверки прошёл"
    return (
        f"скилл «{name}» написан, {tools} инструмент(ов), {verdict}. "
        f"Посмотри {path.as_posix()} и скажи «прими скилл {name}»"
    )


class AuthorSkill(Skill):
    """Пишет новые скиллы руками агента-кодера и складывает их в черновики."""

    meta = SkillMeta(
        name="author",
        description="Пишет новые умения для ассистента и готовит их к подключению",
        version="0.1.0",
        spoken=("автор", "скиллодел", "author"),
    )

    async def on_setup(self) -> None:
        """Прочитать, куда ходить за работой."""
        self._url = str(self.context.setting("url", "")).rstrip("/")
        self._key = str(self.context.setting("api_key", ""))
        self._provider = str(self.context.setting("provider", "claude"))
        self._workdir = str(self.context.setting("workdir", ""))
        self._timeout = float(self.context.setting("timeout", TIMEOUT))
        self._root = self.context.root

    async def health(self) -> HealthStatus:
        """Есть ли куда ходить и с чем."""
        if not self._url:
            return HealthStatus.degraded("не задан url панели")
        if not self._key:
            return HealthStatus.degraded("нет ключа: задай CLI_CLAUDE в .env")
        return HealthStatus.healthy()

    # --- инструменты -------------------------------------------------------

    @tool(
        phrases=[
            "научись {what}",
            "напиши скилл {what}",
            "напиши модуль {what}",
            "сделай скилл {what}",
            "learn to {what}",
            "write a skill {what}",
        ],
        reversible=False,
    )
    async def learn(self, what: str, language: str = "ru") -> ToolResult:
        """Написать себе новое умение и положить его в черновики.

        Занимает около минуты, поэтому уходит в фон: ассистент ответит сразу и
        доложит, когда будет готово. Готовое само не включается — его надо
        принять отдельной командой.

        :param what: чему научиться, своими словами.
        :param language: язык доклада.
        """
        if not self._key or not self._url:
            return ToolResult.failure(
                "панель не настроена: нужен url и ключ CLI_CLAUDE",
                speech={
                    "ru": "Не могу писать скиллы: панель не настроена.",
                    "en": "I can't write skills: the panel isn't configured.",
                },
            )

        job = self._jobs_submit(what, language)
        if job is None:
            return ToolResult.failure(
                "все места заняты",
                speech={
                    "ru": "Сейчас и так три дела в работе, подожди.",
                    "en": "Three things are already running, hold on.",
                },
            )

        return ToolResult.success(
            {"job": job, "what": what},
            speech={
                "ru": "Займусь. Это займёт около минуты, доложу.",
                "en": "I'll write it. About a minute, I'll report back.",
            },
        )

    def _jobs_submit(self, what: str, language: str) -> int | None:
        """Сдать написание в фоновые поручения."""
        job = self.context.jobs.submit(
            f"написать скилл: {what}", self._write(what), language=language
        )
        return job.id if job else None

    @tool(
        phrases=[
            "прими скилл {name}",
            "прими модуль {name}",
            "подключи черновик {name}",
            "accept skill {name}",
        ],
        reversible=False,
    )
    async def accept(self, name: str) -> ToolResult:
        """Принять написанный скилл: перенести из черновиков и подключить.

        Необратимо помечен не из-за переноса файла, а из-за того, что после него
        код начинает работать. Такое решение принимает человек.

        :param name: имя скилла из доклада.
        """
        safe = safe_name(name)
        if not safe:
            return ToolResult.failure(
                f"негодное имя: {name!r}",
                speech={"ru": f"Не знаю черновика {name}.", "en": f"No draft named {name}."},
            )

        source = draft_path(self._root, safe)
        if not source.is_file():
            return ToolResult.failure(
                f"черновика {safe} нет",
                speech={
                    "ru": f"Черновика {safe} не нашёл.",
                    "en": f"Draft {safe} not found.",
                },
            )

        target = self._root / "skills" / safe / "skill.py"
        await asyncio.to_thread(self._move, source, target)
        self.log.info("Черновик %s принят: %s", safe, target)

        connected = await self.context.tools.invoke("core.reload_skill", {"skill": safe})
        if not connected.ok:
            return ToolResult.failure(
                f"файл перенесён, но модуль не поднялся: {connected.error}",
                speech={
                    "ru": f"Файл на месте, а модуль {safe} не запустился. Смотри лог.",
                    "en": f"File moved, but module {safe} failed to start. See the log.",
                },
            )
        return ToolResult.success(
            {"skill": safe, "path": target.as_posix()},
            speech={
                "ru": f"Скилл {safe} принят и подключён.",
                "en": f"Skill {safe} accepted and connected.",
            },
        )

    @staticmethod
    def _move(source: Path, target: Path) -> None:
        """Перенести черновик в рабочий каталог скиллов."""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        source.unlink()
        # Пустой каталог черновика убираем, чтобы список не врал.
        try:
            source.parent.rmdir()
        except OSError:
            pass

    @tool(phrases=["какие черновики", "что написано", "what drafts"], reversible=True)
    async def drafts(self) -> ToolResult:
        """Показать скиллы, написанные и ждущие одобрения."""
        folder = self._root / DRAFTS
        names = sorted(
            item.name for item in folder.glob("*") if (item / "skill.py").is_file()
        ) if folder.is_dir() else []
        if not names:
            return ToolResult.success(
                [], speech={"ru": "Черновиков нет.", "en": "No drafts."}
            )
        listed = ", ".join(names)
        return ToolResult.success(
            names,
            speech={
                "ru": f"Ждут одобрения: {listed}.",
                "en": f"Waiting for approval: {listed}.",
            },
        )

    # --- работа ------------------------------------------------------------

    async def _write(self, what: str) -> str:
        """Заказать скилл у панели, проверить и сохранить. Возвращает доклад."""
        code = await self._draft(build_prompt(what))
        name = skill_name(code)
        path = draft_path(self._root, name)
        await asyncio.to_thread(self._save, path, code)

        findings = await self._check(path)
        if findings:
            # Одна попытка исправления, не больше. Вторая почти всегда означает,
            # что модель ходит по кругу, а платим мы за каждый заход временем.
            self.log.info("Черновик %s не прошёл проверку, прошу исправить", name)
            fixed = await self._draft(repair_prompt(code, findings))
            if skill_name(fixed) == name:
                await asyncio.to_thread(self._save, path, fixed)
                code, findings = fixed, await self._check(path)

        self.log.info("Черновик скилла %s сохранён: %s", name, path)
        return report(name, path, code.count("@tool"), findings)

    async def _draft(self, prompt: str) -> str:
        """Спросить панель и убедиться, что вернулся именно скилл."""
        code = extract_code(await self._ask(prompt))
        wrong = looks_like_skill(code)
        if wrong:
            self.log.warning("Панель вернула не скилл (%s): %s", wrong, code[:200])
            raise ValueError(wrong)
        return code

    async def _check(self, path: Path) -> str:
        """Прогнать по черновику линтер и проверку типов. Пусто — чисто.

        **Проверка типов тут не придирка, а единственное, что ловит главное.**
        На первом же живом запуске агент написал `HealthStatus.unhealthy` —
        метода с таким именем у нас нет — и обратился к часам, которых нет на
        Windows. Код при этом выглядел безупречно, линтер молчал, а mypy нашёл
        обе ошибки за секунду.
        """
        findings: list[str] = []
        for name, args in (
            ("линтер", ["-m", "ruff", "check", str(path)]),
            ("типы", ["-m", "mypy", "--ignore-missing-imports", str(path)]),
        ):
            code, output = await self._run(args)
            if code is None:
                self.log.debug("Проверка «%s» недоступна, пропускаю", name)
                continue
            if code != 0:
                findings.append(f"{name}:\n{output.strip()}")
        return "\n\n".join(findings)

    @staticmethod
    async def _run(args: list[str]) -> tuple[int | None, str]:
        """Запустить проверку тем же интерпретатором. ``None`` — нет такой.

        Тем же самым намеренно: у владельца система живёт не в проектном
        окружении, и чужой `python` из PATH проверил бы не тот код.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError:
            return None, ""
        raw, _ = await process.communicate()
        output = raw.decode("utf-8", errors="replace")
        # Ненайденный модуль — это «проверки нет», а не «проверка провалилась».
        if "No module named" in output:
            return None, ""
        return process.returncode, output

    @staticmethod
    def _save(path: Path, code: str) -> None:
        """Записать черновик, создав каталог."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code, encoding="utf-8")

    async def _ask(self, prompt: str) -> str:
        """Спросить панель и собрать текст ответа.

        **Только потоковый режим.** Нестримовый у панели врёт: возвращает
        HTTP 200 и `success: true`, даже когда не запустилось ничего, — это
        записано в журнале граблей и проверено на живых запросах.
        """
        payload = {
            "message": prompt,
            "stream": True,
            "provider": self._provider,
        }
        if self._workdir:
            payload["projectPath"] = self._workdir

        chunks: list[str] = []
        failure = ""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST",
                f"{self._url}/api/agent",
                headers={"X-API-Key": self._key, "Content-Type": "application/json"},
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    kind = event.get("kind") or event.get("type")
                    if kind == "text":
                        chunks.append(str(event.get("content") or ""))
                    elif kind == "error":
                        failure = str(event.get("content") or event.get("error") or "")
                    elif kind == "complete" and not event.get("success", True):
                        failure = failure or "агент завершился с ошибкой"

        if failure:
            raise RuntimeError(failure)
        return "\n".join(chunk for chunk in chunks if chunk)
