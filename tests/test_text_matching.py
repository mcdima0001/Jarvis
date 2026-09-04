"""Лестница сопоставления: услышанное против списка написанного.

Проверяются не абстрактные свойства, а те самые случаи, ради которых каждое
правило и появилось: «кто такой трамп» открывало Telegram, «открой гитхап»
запускало Ample Guitar, «Яндекс музыку» открывала поиск Яндекса. Все они
приехали сюда из скиллов вместе с кодом — раньше эти правила были написаны
по отдельности в четырёх местах и проверялись тоже по отдельности.
"""

from __future__ import annotations

from jarvis.core.text import (
    best_match,
    closeness,
    forms,
    shared_word,
    sounds_alike,
    starts,
    stem,
    touches,
)


# --- формы написания --------------------------------------------------------


def test_forms_cover_case_alphabet_and_separators() -> None:
    """Одно имя в падеже, в другом алфавите и без разделителей — это одно имя.

    Именительный падеж при этом не восстанавливается и не должен: окончание
    срезается с обеих сторон, и совпадают не слова, а их основы.
    """
    assert forms("Маме") & forms("Мама")
    assert "настяко" in forms("Настя Ко")
    assert "sasha" in forms("Саша")
    # Заглавная буква переводится наравне со строчной, иначе получалась смесь
    # алфавитов: «Настя Ко» превращалась в «нastyaкo».
    assert "nastyako" in forms("Настя Ко")


def test_forms_drop_the_too_short() -> None:
    """Короткая форма подойдёт к чему угодно, поэтому её не берём."""
    assert forms("Ко") == set()


def test_stem_removes_the_case_ending() -> None:
    """«На ютубе», «в гитхабе», «открой почту» — падеж сравнению не помеха."""
    assert stem("почту") == stem("почта") == "почт"


# --- совпадение краем -------------------------------------------------------


def test_touches_matches_by_either_edge() -> None:
    """«Обс» находит «OBS Studio», «торрент» — «qBittorrent»."""
    assert touches("obs", "obsstudio")
    assert touches("torrent", "qbittorrent")


def test_touches_ignores_the_middle() -> None:
    """Кусок в середине — не совпадение, и это стоило двух разборов.

    «telegramdesktop» содержит «кто», и вопрос «кто такой трамп» открывал
    Telegram; «блокнот» содержит «окно».
    """
    assert not touches("kto", "telegramdesktop")
    assert not touches("окно", "блокнот")


def test_starts_is_stricter_than_touches() -> None:
    """Началом — да, концом — нет: хвост названия несёт смысл.

    Иначе «музыка» подошла бы к любому музыкальному сайту, а фамилия адресата
    стала бы достаточным основанием, чтобы написать человеку.
    """
    assert starts("яндекс", "яндексмузыка")
    assert touches("музыка", "яндексмузыка")
    assert not starts("музыка", "яндексмузыка")


# --- созвучие ---------------------------------------------------------------


def test_sounds_alike_survives_transliteration() -> None:
    """«Фотошоп» и «photoshop» пишутся по-разному, а звучат одинаково."""
    assert sounds_alike("фотошоп", "photoshop")
    assert sounds_alike("МаршалТех", "MarshallTech")


def test_short_skeleton_is_not_trusted() -> None:
    """У «YouTube» костяк равен «tb» и совпал бы со слишком многим."""
    assert not sounds_alike("YouTube", "ЮТьюб")


# --- нечёткое сравнение -----------------------------------------------------


def test_closeness_ignores_lengths_too_different() -> None:
    """Короткое «окно» иначе находит «блокнот» с похожестью 0.73."""
    assert closeness("окно", "блокнот") > 0.6
    assert closeness("окно", "блокнот", balance=0.7) == 0.0


def test_shared_long_word_is_a_kinship() -> None:
    """«Логотип YouTube» и «YouTube Главная» — про одно и то же."""
    assert shared_word("логотип YouTube", "YouTube Главная")
    assert not shared_word("Ещё", "скопировать")


# --- лестница целиком -------------------------------------------------------


def test_exact_form_wins_over_everything() -> None:
    """Первая ступень — точное совпадение любой формы."""
    assert best_match("маме", ["Мама", "Мамонт"], similarity=0.8) == "Мама"


def test_shortest_wins_among_equals() -> None:
    """«Мама» — это «Мама», а не «Мама Юли»."""
    assert best_match("мама", ["Мама Юли", "Мама"], similarity=0.8) == "Мама"


def test_longest_wins_when_asked() -> None:
    """«Яндекс музыка» иначе проигрывает записи «Яндекс».

    Живой промах: открывался поиск вместо музыки — какая запись попадётся в
    словаре первой, такая и выигрывала. Названное целиком до этой развилки не
    доходит: «яндекс музыку» совпадает с «яндекс музыка» точно, ещё на первой
    ступени. А вот у недоговорённого («яндекс муз») подходят обе записи, и
    сайтам нужна длинная — в отличие от чатов, где «Мама» правильнее «Мамы Юли».
    """
    sites = ["яндекс", "яндекс музыка"]
    assert best_match("яндекс муз", sites, similarity=0.8, edges=starts) == "яндекс"
    assert (
        best_match(
            "яндекс муз", sites, similarity=0.8, edges=starts, prefer=lambda s: -len(s)
        )
        == "яндекс музыка"
    )


def test_skeleton_beats_fuzzy_matching() -> None:
    """Созвучие точнее нечёткого сравнения, поэтому стоит выше него.

    «МаршалТех» и «MarshallTech» — одно название, а нечёткое сравнение между
    алфавитами даёт около нуля.
    """
    assert best_match("MarshallTech", ["МаршалТех", "Marshall Games"], similarity=0.8) == "МаршалТех"


def test_nothing_is_returned_when_unsure() -> None:
    """Не узнали — отказ, а не ближайший сосед по алфавиту."""
    assert best_match("совершенно другое", ["Мама", "Настя Ко"], similarity=0.8) is None


def test_voiced_ending_heard_as_voiceless() -> None:
    """Whisper глушит звонкие на конце: «гитхаб» слышится как «гитхап»."""
    assert best_match("гитхап", ["гитхаб", "почта"], similarity=0.8) == "гитхаб"
