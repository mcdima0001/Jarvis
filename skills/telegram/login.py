"""Разовый вход в аккаунт Telegram: ``python skills/telegram/login.py``

Внутри Jarvis этого не сделать: Телеграм присылает код в приложение, его надо
ввести руками, а запуск ассистента не должен зависать в ожидании ввода. Поэтому
вход — отдельная команда, которую выполняют один раз.

Ключи берутся из того же конфига, что и у скилла (`api_id`, `api_hash` из
`.env` через `${...}`), так что второй раз их указывать не нужно.

Файл сессии, который появится после входа, **равносилен входу в аккаунт**: ни
пароля, ни кода к нему уже не требуется. Он лежит в `memory/`, который целиком
в `.gitignore`, — и делиться им нельзя ни с кем.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Скилл лежит вне пакета, поэтому корень проекта добавляется в путь вручную —
# так же, как это делает загрузчик скиллов.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis.core.config import load_config  # noqa: E402


def main() -> int:
    """Спросить код и сохранить сессию."""
    try:
        from telethon import TelegramClient
    except ImportError:
        print('Telethon не установлен. Выполни: pip install -e ".[telegram]"', file=sys.stderr)
        return 1

    config = load_config()
    settings = config.skills.settings_for("telegram")
    api_id = int(settings.get("api_id") or 0)
    api_hash = str(settings.get("api_hash") or "")
    if not api_id or not api_hash:
        print(
            "Нет ключей. Добавь в .env:\n"
            "  JARVIS_TELEGRAM_API_ID=...\n"
            "  JARVIS_TELEGRAM_API_HASH=...\n"
            "Взять их можно на my.telegram.org → API development tools.",
            file=sys.stderr,
        )
        return 2

    session = config.root / str(settings.get("session") or "memory/telegram.session")
    session.parent.mkdir(parents=True, exist_ok=True)

    print(f"Вход в Telegram. Сессия будет сохранена в {session}")
    with TelegramClient(str(session.with_suffix("")), api_id, api_hash) as client:
        me = client.get_me()
        name = getattr(me, "username", "") or getattr(me, "first_name", "")
        print(f"Готово: вошли как {name}. Больше эта команда не понадобится.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
