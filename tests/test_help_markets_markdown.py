# -*- coding: utf-8 -*-
"""Regression test: `_markets_help_text()` must be parseable as Telegram
Markdown V1.

Background: команда `/help markets` молча падала в проде — юзер видел
ответ на `/help`, но на `/help markets` ничего. Причина — непарные `_`
внутри `*RANDOM_WALK*` и `MEAN_REVERTING` рвали Telegram-парсер
(«can't parse entities»), aiogram логировал TelegramBadRequest и юзер
получал пустоту.

Этот тест читает `main.py` через AST (без `import main`, чтобы не тянуть
aiogram / matplotlib / FinBERT в test runner) и проверяет:
    1. вне backtick-кода не должно быть символов `_` — они ломают MD V1;
    2. количество `*` вне кода должно быть чётным (парные жирные блоки);
    3. количество `` ` `` должно быть чётным (парные code-span'ы).
"""
from __future__ import annotations

import ast
import os
import re
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _extract_markets_help_text() -> str:
    """Достаёт return value из `_markets_help_text()` без import main.py.

    Тело функции — это конкатенация литералов строк, которую CPython
    сворачивает в один `Constant` на этапе парсинга. `ast.literal_eval`
    безопасно достаёт значение.
    """
    src_path = os.path.join(REPO_ROOT, "main.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_markets_help_text":
            for stmt in node.body:
                if isinstance(stmt, ast.Return) and stmt.value is not None:
                    return ast.literal_eval(stmt.value)
    raise AssertionError("_markets_help_text() not found in main.py")


def _strip_code_spans(text: str) -> str:
    """Убирает `...` code-span'ы — внутри них MD V1 не парсит markup."""
    return re.sub(r"`[^`\n]*`", "", text)


class TestMarketsHelpMarkdown(unittest.TestCase):
    """Защита от регрессии: команда /help markets обязана дойти до юзера."""

    @classmethod
    def setUpClass(cls):
        cls.text = _extract_markets_help_text()
        cls.outside_code = _strip_code_spans(cls.text)

    def test_text_is_non_empty(self):
        self.assertGreater(len(self.text), 500, "Markets help looks empty/truncated")

    def test_no_unescaped_underscores_outside_code_spans(self):
        """Telegram Markdown V1 трактует `_` как italic. Непарный `_` →
        ошибка `can't parse entities` → ответ молча проваливается.

        Если нужен литеральный `_` в тексте — используй backticks
        (`` `MEAN_REVERTING` ``) или замени на дефис.
        """
        unescaped = self.outside_code.count("_")
        # Find offending positions for a helpful failure message
        offsets = [
            f"@{m.start()}: ...{self.outside_code[max(0, m.start() - 15):m.end() + 15]!r}..."
            for m in re.finditer(r"_", self.outside_code)
        ]
        self.assertEqual(
            unescaped,
            0,
            "Unescaped `_` outside code spans breaks Telegram Markdown V1.\n"
            "Offending occurrences:\n  " + "\n  ".join(offsets),
        )

    def test_asterisks_are_balanced(self):
        """`*bold*` должны быть парными — иначе MD V1 не закрывает разметку."""
        n = self.outside_code.count("*")
        self.assertEqual(
            n % 2,
            0,
            f"Found {n} asterisks outside code spans — must be even (paired *bold*).",
        )

    def test_backticks_are_balanced(self):
        """`` `code` `` должны быть парными — иначе MD V1 ругается."""
        n = self.text.count("`")
        self.assertEqual(
            n % 2,
            0,
            f"Found {n} backticks total — must be even (paired `code`).",
        )

    def test_fits_in_one_telegram_message(self):
        """Telegram-лимит на сообщение — 4096 символов."""
        self.assertLess(
            len(self.text),
            4096,
            f"Markets help is {len(self.text)} chars — exceeds Telegram 4096-char limit",
        )

    def test_mentions_random_walk_label(self):
        """Smoke: текст всё ещё упоминает основные режимы рынка."""
        for label in ("TRENDING", "MEAN-REVERTING", "CHAOTIC"):
            self.assertIn(label, self.text, f"Missing label `{label}` in markets help")


if __name__ == "__main__":
    unittest.main()
