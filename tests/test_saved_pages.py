"""Планы скилла `page` на настоящих страницах сайтов, сохранённых владельцем.

Самопроверка расширения строит страницы руками — такими, какими мы их себе
представляем. Здесь страницы такие, какими их сделал сайт: копия ложится в
`extension/pagetest/pages/`, план собирается **тем же кодом, что в скилле**, и
выполняется в jsdom (`extension/pagetest/run.js`). Нажатия не исполняются, а
записываются — по ним и проверяем, куда бы ткнул Jarvis.

Нужны node и jsdom: `npm install --include=dev --prefix extension/pagetest`
(`--include=dev` обязателен, если в окружении `NODE_ENV=production` — так на
сервере, и npm без него молча ставит пустоту). Нет их — тесты пропускаются.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_HARNESS = _ROOT / "extension" / "pagetest"
_PAGES = _HARNESS / "pages"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not (_HARNESS / "node_modules" / "jsdom").is_dir(),
    reason="нет node или jsdom — сохранённые страницы не прогнать",
)


def _load() -> Any:
    path = _ROOT / "skills" / "browser" / "page" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_page_saved", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


page = _load()


def _run(name: str, url: str, plan: list[dict[str, Any]]) -> dict[str, Any]:
    done = subprocess.run(
        ["node", str(_HARNESS / "run.js"), str(_PAGES / name), url, json.dumps(plan, ensure_ascii=False)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _search_plan(host: str, names: list[str]) -> list[dict[str, Any]]:
    """План выдачи поиска так, как его собирает `_from_results`."""
    skill = page.PageSkill()
    skill._own, skill._known = {}, {}
    skill._seconds, skill._volume_step = 10, 0.1
    steps = [{"item": names, "hint": list(page.PLAY_HINTS), "play": True}]
    return [{"suggest": list(page.DID_YOU_MEAN)}, *skill._with_top(host, steps)]


def test_liked_songs_start_from_their_own_list_not_the_wave() -> None:
    """22.10.2026: «Мне нравится» в коллекции — лента без общей кнопки, над ней волна."""
    answer = _run("yandex-collection.html", "https://music.yandex.ru/collection",
                  page.liked_plan("music.yandex.ru"))

    assert answer["result"]["done"] == "item", answer["result"]
    first = answer["clicks"][0]
    assert "PlayButtonWithCover_playButton" in first["classes"], first
    assert "PlaylistWithTracks" in first["within"], "нажато не в ленте «Мне нравится»"
    for click in answer["clicks"]:
        assert "CollectionVibeAgent" not in click["within"] + click["classes"], "заиграла бы волна"
        assert "CommonControlsBar" not in click["within"] + click["classes"], "лайк треку"
        assert "PlayerBar" not in click["within"], "кнопка плеера внизу"


def test_misheard_search_presses_the_site_suggestion_first() -> None:
    """22.10.2026, 09:27: «Arctic Mankins 505» — подсказка сайта, а не «Моя волна» по ослышке."""
    answer = _run(
        "yandex-search-misheard.html",
        "https://music.yandex.ru/search?text=Arctic+Mankins+505",
        _search_plan("music.yandex.ru", ["arctic mankins 505"]),
    )

    assert answer["result"]["done"] == "suggest", answer["result"]
    assert answer["clicks"][0]["label"] == "Arctic Monkeys 505"


def test_misheard_search_never_plays_the_wave_without_the_suggestion() -> None:
    """Подсказку уже нажимали: шаг выдачи сам по себе волну не нажимает."""
    plan = [step for step in _search_plan("music.yandex.ru", ["arctic mankins 505"]) if "suggest" not in step]

    answer = _run("yandex-search-misheard.html", "https://music.yandex.ru/search?text=Arctic+Mankins+505", plan)

    for click in answer["clicks"]:
        assert "QueryToVibe" not in click["within"] + click["classes"], f"заиграла бы волна: {click}"
        assert "SearchCorrectedText" not in click["within"] + click["classes"], "подсказка — не трек"


def test_liked_songs_survive_a_renamed_play_button() -> None:
    """Сайт переименовал класс кнопки — по названию «Мне нравится» всё равно лента, не волна."""
    plan = page.liked_plan("music.yandex.ru")
    plan[0].pop("prefer", None)

    answer = _run("yandex-collection.html", "https://music.yandex.ru/collection", plan)

    assert answer["clicks"], answer["result"]
    assert "PlaylistWithTracks" in answer["clicks"][0]["within"]
    assert all("CollectionVibeAgent" not in c["within"] + c["classes"] for c in answer["clicks"])
