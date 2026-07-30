# Скиллы

Всё, что умеет Jarvis, живёт здесь. Ядро не знает, какие скиллы существуют:
оно находит их при старте, читает объявленные инструменты и фразы и добавляет
в общий каталог.

## Как добавить свой

Создай `skills/имя/skill.py` (или просто `skills/имя.py`) и опиши класс:

```python
from jarvis.core.contracts import ToolResult
from jarvis.core.skills import Skill, SkillMeta
from jarvis.core.tools import tool


class TimerSkill(Skill):
    meta = SkillMeta(name="timer", description="Таймеры и напоминания")

    async def on_setup(self) -> None:
        self._minutes = int(self.context.setting("default_minutes", 5))

    @tool(phrases=["поставь таймер на {minutes} минут"])
    async def start_timer(self, minutes: int) -> ToolResult:
        """Запустить таймер.

        :param minutes: длительность в минутах.
        """
        return ToolResult.success(minutes, speech=f"Таймер на {minutes} минут.")
```

Перезапусти Jarvis — команда уже работает. Ни ядро, ни конфиг править не нужно.
Проверить: `python -m jarvis --check`.

## Что даёт декоратор `@tool`

Из сигнатуры и докстринга автоматически получаются:

* **имя** — `timer.start_timer`;
* **описание** — первая строка докстринга;
* **JSON Schema** — из аннотаций типов и строк `:param:`;
* **фразы** — для маршрутизации без обращения к LLM (в том числе с
  подстановкой `{minutes}`).

Тот же каталог видит и языковая модель через function-calling.

## Правила

* **Скилл не импортирует другой скилл.** Нужна чужая команда — вызови её по
  имени: `await self.tools.invoke("windows.launch_program", {...})`.
* **Событие ≠ команда.** Факт («датчик показал 23°») публикуется в шину.
  Запрос, на который нужен ответ («какая температура»), — это инструмент.
* **Фоновые задачи — только через scope:** `self.context.scope.spawn(...)`.
  Тогда они гарантированно остановятся вместе со скиллом.
* **`speech` — это то, что произнесут вслух.** Технические подробности (пути,
  переменные окружения, названия файлов) держи в `error` и `value`, а в `speech`
  пиши живую русскую фразу. Латиница переводится в русское чтение
  автоматически, но «добавь ключ в файл .env» вслух всё равно звучит хуже, чем
  «добавь ключ в настройки».
* **Настройки — в `config.yaml`**, секция `skills.settings.<имя>`. Скилл должен
  работать и без неё, на значениях по умолчанию.
* **Секреты — в `.env`**, а в конфиге только ссылка `${JARVIS_MY_TOKEN}`.

## Что здесь сейчас

| Скилл      | Состояние | Инструменты |
|------------|-----------|-------------|
| `browser`  | **работает** | `open_site`, `search`, `close`, `close_tab` (+ служебные `page_target`, `page_run`, `page_go`, `page_probe`) |
| `page`     | **работает**, нужно расширение | `control`, `press`, `play_item`, `type_in` и голосовые обёртки: пауза, следующий трек, лайк, перемотка |
| `windows`  | **работает**, только Windows | `launch_program`, `close_program`, `kill_program`, `list_programs`, `set_volume`, `lock` |
| `search`   | **работает** | `web_search`, `answer` |
| `weather`  | **работает** | `now`, `forecast` |
| `memory`   | **работает** | `remember`, `recall`, `set_preference`, `about_me` |
| `telegram` | **работает**, нужен вход в аккаунт | `send_message`, `get_recent_chats`, `read_chat`, `summarize_chat` |
| `esp32`    | заглушка  | `set_light`, `get_temperature`, `set_mode`, `get_humidity` |
| `youtube`  | `play_video` **работает** (поиск + нажатие первого ролика), `search_video` ждёт ключа | `search_video`, `play_video` |

Места, где нужна реальная интеграция, помечены в коде как `TODO`.

Пара `browser` и `page` — образец разделения обязанностей между скиллами.
Вкладки, адреса и связь с расширением — у `browser`; что нажимать внутри
страницы — у `page`. Друг друга они не импортируют: `page` зовёт
`browser.page_run` по имени через реестр, и, если браузерного скилла нет,
честно отказывает вместо падения.
