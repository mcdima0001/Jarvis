"""Резка текста, который приходит кусками, на предложения — чтобы говорить по ходу.

Модель пишет ответ по слову, а синтезу нужна фраза целиком: по обрывку он
расставит интонацию так, будто предложение кончилось. Поэтому куски копятся и
отдаются, как только сложилось законченное предложение.

**Совсем короткое не отдаётся отдельно** (`MIN_CHARS`). «Да.» или «Сэр.»
отдельной репликой — это лишний запрос к синтезу и заметная пауза посреди
ответа; такое склеивается со следующим предложением.
"""

from __future__ import annotations

import re

#: Короче этого предложение не отдаётся само по себе, а ждёт следующего.
MIN_CHARS = 24

#: Конец предложения: знак и пробел после него. Пробел обязателен — иначе
#: «3.5 градуса» резалось бы посреди числа, а конец текста ещё не пришёл.
_END = re.compile(r"[.!?…]+[»\")]*\s+|\n+")


class SentenceSplitter:
    """Копит куски текста и отдаёт готовые предложения."""

    def __init__(self, *, min_chars: int = MIN_CHARS) -> None:
        self._min_chars = min_chars
        self._buffer = ""

    def push(self, piece: str) -> list[str]:
        """Добавить кусок; вернуть предложения, которые уже сложились."""
        self._buffer += piece
        ready: list[str] = []
        start = 0
        for match in _END.finditer(self._buffer):
            if len(self._buffer[start:match.end()].strip()) < self._min_chars:
                continue
            ready.append(self._buffer[start:match.end()].strip())
            start = match.end()
        self._buffer = self._buffer[start:]
        return ready

    def flush(self) -> str:
        """Всё, что осталось, когда текст кончился."""
        tail, self._buffer = self._buffer.strip(), ""
        return tail
