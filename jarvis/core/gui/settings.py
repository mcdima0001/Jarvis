"""Настройки, которые меняет панель: ключи, модули, права, лог.

Всё здесь — чистые функции над текстом файлов, без сети и без приложения. Так
их можно проверить на любой машине, а панель остаётся тонкой: прочитала файл,
отдала функции, записала результат.

**Ключи не показываются никогда, даже частично.** Панель знает о ключе три
вещи: задан ли он, какой он длины и где в конфиге используется. Этого хватает,
чтобы понять «OpenAI не отвечает, потому что ключа нет», и не хватает, чтобы
ключ утёк через снимок экрана или историю браузера.

**Файлы правятся строкой, а не пересобираются.** И `.env`, и `config.yaml`
полны комментариев владельца; разобрать YAML и записать обратно значило бы
стереть их все. Поэтому меняется ровно одна строка, остальное остаётся байт в
байт.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

#: ``${VAR}`` и ``${VAR:-значение}`` — так конфиг ссылается на `.env`.
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")
#: Имя переменной окружения: иное в `.env` писать нельзя.
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: Имя скилла — имя каталога.
_SKILL = re.compile(r"^[A-Za-z0-9_-]+$")

#: Сколько байт лога отдавать за раз. Файл за день бывает мегабайтами.
LOG_CHUNK = 200_000


@dataclass(frozen=True, slots=True)
class KeyInfo:
    """Что панель знает о ключе. Значения тут нет намеренно."""

    name: str
    present: bool
    length: int
    #: Какие файлы конфига на него ссылаются.
    used_by: tuple[str, ...]


def read_env(text: str) -> dict[str, str]:
    """Разобрать `.env` так же, как это делает загрузчик конфига."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def referenced_keys(sources: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    """Какие переменные упоминаются в каких файлах конфига."""
    found: dict[str, list[str]] = {}
    for source, text in sources.items():
        for match in _REFERENCE.finditer(text):
            users = found.setdefault(match.group(1), [])
            if source not in users:
                users.append(source)
    return {name: tuple(users) for name, users in found.items()}


def key_list(env_text: str, sources: Mapping[str, str]) -> list[KeyInfo]:
    """Все ключи: заданные в `.env` и те, на которые ссылается конфиг."""
    env = read_env(env_text)
    used = referenced_keys(sources)
    names = sorted(set(env) | set(used))
    return [
        KeyInfo(
            name=name,
            present=bool(env.get(name)),
            length=len(env.get(name, "")),
            used_by=used.get(name, ()),
        )
        for name in names
    ]


def update_env(text: str, name: str, value: str) -> str:
    """Задать, заменить или (пустым значением) удалить ключ в тексте `.env`.

    :raises ValueError: имя не годится для переменной или значение многострочное.
    """
    if not _NAME.match(name):
        raise ValueError(f"недопустимое имя переменной: {name!r}")
    if "\n" in value or "\r" in value:
        raise ValueError("значение ключа не может содержать перевод строки")
    value = value.strip()
    lines = text.splitlines()
    kept: list[str] = []
    replaced = False
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key == name:
            if value and not replaced:
                kept.append(f"{name}={value}")
            replaced = True
            continue
        kept.append(line)
    if value and not replaced:
        kept.append(f"{name}={value}")
    return "\n".join(kept) + "\n"


def set_disabled(yaml_text: str, names: Iterable[str]) -> str:
    """Переписать строку ``disabled:`` в секции ``skills:`` главного конфига.

    Комментарий на той же строке сохраняется. Секция без такой строки — ошибка:
    дописывать в чужой YAML наугад опаснее, чем отказать.

    :raises ValueError: нет секции или строки, либо имя скилла недопустимо.
    """
    listed = sorted(set(names))
    for name in listed:
        if not _SKILL.match(name):
            raise ValueError(f"недопустимое имя скилла: {name!r}")
    lines = yaml_text.split("\n")
    in_skills = False
    for index, line in enumerate(lines):
        if re.match(r"^\S", line):
            in_skills = line.startswith("skills:")
            continue
        match = re.match(r"^(\s+)disabled:\s*(\[[^\]]*\])?(\s*#.*)?\s*$", line) if in_skills else None
        if match:
            indent, _, comment = match.groups()
            lines[index] = f"{indent}disabled: [{', '.join(listed)}]{comment or ''}"
            return "\n".join(lines)
    raise ValueError("в config.yaml нет строки skills.disabled")


def launcher_level(data: bytes) -> str | None:
    """Какие права просит собранный Jarvis.exe: манифест лежит в нём текстом."""
    for level in ("requireAdministrator", "highestAvailable", "asInvoker"):
        if level.encode("ascii") in data:
            return level
    return None


def is_admin() -> bool:
    """Запущен ли процесс с правами администратора."""
    if sys.platform == "win32":
        try:
            return bool(ctypes.WinDLL("shell32").IsUserAnAdmin())
        except OSError:
            return False
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def tail(path: Path, offset: int, *, chunk: int = LOG_CHUNK) -> tuple[str, int]:
    """Дочитать лог с места, где остановились.

    :param offset: сколько байт уже отдано; отрицательное — начать с хвоста.
        Файл стал короче (новые сутки, новый файл) — тоже с хвоста.
    :return: новый текст и новое смещение.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return "", 0
    if offset < 0 or offset > size:
        offset = max(0, size - chunk)
    with path.open("rb") as file:
        file.seek(offset)
        data = file.read(chunk)
    # Не отдавать оборванную букву: UTF-8 кириллица — два байта.
    cut = data.rfind(b"\n") + 1 if len(data) == chunk else len(data)
    data = data[:cut] if cut else data
    return data.decode("utf-8", errors="replace"), offset + len(data)
