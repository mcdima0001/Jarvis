# Jarvis

Голосовой ассистент для управления домашней студией: компьютер, ESP32, свет,
датчики, Telegram, YouTube, поиск.

Это **фундамент**, а не готовый продукт. Ядро собрано так, чтобы через год новая
возможность добавлялась одним файлом в `skills/`, а через несколько лет любую
часть (LLM, Whisper, TTS, память, шина, роутер) можно было заменить, не переписывая
остальное.

## Быстрый старт

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

cp .env.example .env        # ключи и токены — только здесь
.venv/bin/python -m jarvis --check
```

`--check` собирает всю систему, печатает каталог инструментов и выходит.
Ни моделей, ни сети, ни API-ключа для этого не нужно.

### Режимы запуска

```bash
python -m jarvis                               # полный запуск
python -m jarvis --check                       # отчёт о сборке, без запуска
python -m jarvis --say "включи игровой режим"   # одна команда текстом
python -m jarvis --log-level DEBUG             # подробный лог
```

### Голос и модель (опционально)

```bash
pip install -e ".[stt,tts]"      # faster-whisper и Piper
```

Модели кладутся в `models/whisper` и `models/piper` (пути — в `config/config.yaml`).
Без них Jarvis работает, просто не слышит и не говорит: поднимаются заглушки,
в лог уходит предупреждение.

Ключ OpenRouter — в `.env`, переменная `JARVIS_OPENROUTER_KEY`. Без него
свободный диалог отключён, а команды по фразам работают как обычно.

## Структура

```
jarvis/core/       ядро: конфиг, шина, инструменты, роутер, скиллы, LLM, память, аудио
skills/            плагины — всё, что Jarvis умеет (см. skills/README.md)
config/config.yaml все настройки; в коде нет ни путей, ни ключей
memory/            память: documents/*.json и journals/*.jsonl
models/, logs/     модели и логи (в .gitignore)
tests/             тесты на швы архитектуры
```

## Как это устроено

Полный разбор решений и обоснования — в [ARCHITECTURE.md](ARCHITECTURE.md).
Коротко:

* **Два канала связи.** Факты идут в шину событий (`sensor.temperature.changed`),
  команды — через реестр инструментов (`await tools.invoke("esp32.get_temperature")`).
  Скиллы никогда не импортируют друг друга.
* **Роутер отдаёт намерение, а не скилл.** Цепочка резолверов: точные фразы →
  синонимы → LLM → диалог. Типовые команды студии до сети не доходят.
* **Провайдер LLM тонкий.** `complete()` и всё. Задачи (`ask`, `summarize`,
  `extract_intent`) написаны один раз в `LLMService`, поэтому новый провайдер —
  это один метод.
* **Скилл = файл в `skills/`.** Инструменты, фразы и JSON Schema выводятся из
  сигнатур и докстрингов. Регистрировать вручную ничего не нужно.

## Добавить возможность

Создай `skills/имя/skill.py`:

```python
from jarvis.core.contracts import ToolResult
from jarvis.core.skills import Skill, SkillMeta
from jarvis.core.tools import tool


class TimerSkill(Skill):
    meta = SkillMeta(name="timer", description="Таймеры")

    @tool(phrases=["поставь таймер на {minutes} минут"])
    async def start_timer(self, minutes: int) -> ToolResult:
        """Запустить таймер.

        :param minutes: длительность в минутах.
        """
        return ToolResult.success(minutes, speech=f"Таймер на {minutes} минут.")
```

Перезапусти — команда работает. Ядро и конфиг не трогаются.
Подробнее: [skills/README.md](skills/README.md).

## Тесты

```bash
.venv/bin/python -m pytest
```

Тесты покрывают именно швы: доставку и изоляцию событий, вывод схем и вызов
инструментов, порядок резолверов (в том числе что известная фраза **не** доходит
до LLM), загрузку и выгрузку скиллов, подстановку переменных в конфиге.

## Состояние

| Часть | Состояние |
|---|---|
| Шина событий, реестр инструментов, роутер, диспетчер | работает |
| Скиллы: автозагрузка, scope, `reload` | работает |
| Конфигурация, логирование, память | работает |
| LLM: сервис, профили, OpenRouter | работает (нужен ключ) |
| STT (faster-whisper), TTS (Piper) | адаптеры готовы, нужны модели |
| VAD, wake word | протоколы и заглушки, реализаций пока нет |
| Скиллы ESP32/Windows/Telegram/YouTube/Search | заглушки с `TODO` |
| Скилл Memory | работает |
