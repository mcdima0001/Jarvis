"""Облачный синтез Fish Audio.

Ни ключа, ни сети: HTTP подделан. Проверяется то, что решает код, — как собран
запрос и как разобран ответ.
"""

from __future__ import annotations

import io
import wave

import httpx
import pytest

from jarvis.core.tts.backends import FishBackend, build_backend

RATE = 24000


def _wav(seconds: float = 0.5, rate: int = RATE) -> bytes:
    """Ответ сервиса: звук приходит готовым WAV."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\0\0" * int(rate * seconds))
    return buffer.getvalue()


def _backend(handler, **kwargs) -> FishBackend:
    """Собрать движок на поддельном HTTP."""
    backend = FishBackend(api_key="test-key", **kwargs)
    backend.prepare("", "ru")
    backend._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers=dict(backend._client.headers),
    )
    return backend


def test_model_goes_into_the_header_not_the_body() -> None:
    """Модель называется заголовком — в теле её молча игнорируют.

    Это предупреждение самой Fish Audio: запрос, который будто не замечает
    выбранную модель, обычно тот, где её положили в JSON.
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["header"] = request.headers.get("model")
        seen["body"] = request.read()
        return httpx.Response(200, content=_wav())

    backend = _backend(handler, model="s2.1-pro-free")
    backend.synthesize("Готово.", "", "ru")

    assert seen["header"] == "s2.1-pro-free"
    assert b"s2.1-pro-free" not in seen["body"], "модель уехала в тело запроса"


def test_wav_is_asked_for_and_read_as_is() -> None:
    """Просим самоописательный формат и берём частоту из него.

    Задать её самим значило бы держать согласованными две записи одного и того
    же; ошибиться — получить голос бурундука, а молча такое лечится плохо.
    """
    import json

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.read()))
        # Сервис вправе прислать не ту частоту, которую просили.
        return httpx.Response(200, content=_wav(rate=16000))

    backend = _backend(handler)
    pcm, rate = backend.synthesize("Готово.", "", "ru")

    assert seen["format"] == "wav"
    assert seen["latency"] == "low", "реплику ждёт человек, а не файл на диске"
    assert rate == 16000, "частота берётся из ответа, а не из наших ожиданий"
    assert len(pcm) == 16000 * 2 // 2


def test_voice_is_passed_when_chosen() -> None:
    """Выбранный голос уходит идентификатором, пустой — не уходит вовсе.

    Без `reference_id` сервис отвечает голосом по умолчанию: так его можно
    послушать, ещё ничего не выбрав.
    """
    import json

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.read()))
        return httpx.Response(200, content=_wav())

    backend = _backend(handler)
    backend.synthesize("Раз.", "abc123", "ru")
    backend.synthesize("Два.", "", "ru")

    assert seen[0]["reference_id"] == "abc123"
    assert "reference_id" not in seen[1]


def test_missing_credit_is_explained() -> None:
    """402 — не «сеть барахлит», а «модель платная».

    Живой случай: `s2.1-pro` отвечает 402, потому что API-кредит считается
    отдельно от кредита платформы. Ответ сервиса попадает в текст ошибки, иначе
    причину пришлось бы искать в их консоли.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, text="Insufficient API credit")

    backend = _backend(handler)
    with pytest.raises(RuntimeError, match="402"):
        backend.synthesize("Готово.", "", "ru")


def test_no_key_is_refused_before_the_first_reply() -> None:
    """Без ключа отказываемся при подготовке, а не молчим в ответ на реплику.

    `prepare` зовётся при запуске: там отказ виден в логе и приводит к откату
    на местный голос.
    """
    backend = FishBackend(api_key="")

    with pytest.raises(RuntimeError, match="без ключа"):
        backend.prepare("", "ru")


def test_backend_is_built_by_name() -> None:
    """Fish создаётся как остальные движки, без особого пути."""
    from pathlib import Path

    backend = build_backend("fish", Path("models"), api_key="test-key")

    assert isinstance(backend, FishBackend)
    assert backend.engine == "fish"
