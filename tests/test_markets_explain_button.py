# -*- coding: utf-8 -*-
"""Тесты для кнопки «📖 Что значат эти слова?» под `/markets`.

Покрывают:
  • `_markets_section_keyboard(..., user_id=...)` — добавляет ряд с
    кнопкой `mktexplain:<uid>` если передан user_id; без user_id
    кнопки нет (legacy-режим для `_markets_signal_keyboard`).
  • `_markets_glossary_text` — содержит все термины из /markets-вывода
    (S/R, MA-триггеры, σ̂, Hurst, Markov, quant и т.д.), влезает в
    Telegram-лимит 4096 символов, безопасен для MD V1.
  • `handle_markets_explain_callback` — UID-guard, send_message с
    глоссарием в чат-каноник.
"""
from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import aiogram  # noqa: F401

    HAS_AIOGRAM = True
except Exception:
    HAS_AIOGRAM = False

# Заглушка BOT_TOKEN — main.py падает при импорте без него.
os.environ.setdefault("BOT_TOKEN", "test:test")


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestMarketsExplainKeyboard(unittest.TestCase):
    """Кнопка `📖 Что значат эти слова?` появляется только если передан user_id."""

    @classmethod
    def setUpClass(cls):
        from main import _markets_section_keyboard  # noqa: PLC0415

        cls._kb = staticmethod(_markets_section_keyboard)

    def test_no_user_id_no_explain_button(self):
        """Без user_id (legacy-вызов `_markets_signal_keyboard`) кнопки нет."""
        kb = self._kb(is_enabled=False, current="summary")
        # Раскладываем все кнопки в плоский список и ищем по callback_data.
        all_buttons = [btn for row in kb.inline_keyboard for btn in row]
        cbdata = [btn.callback_data for btn in all_buttons]
        self.assertFalse(any(cd and cd.startswith("mktexplain:") for cd in cbdata))

    def test_with_user_id_adds_explain_button(self):
        kb = self._kb(is_enabled=False, current="summary", user_id=42)
        all_buttons = [btn for row in kb.inline_keyboard for btn in row]
        explain = [btn for btn in all_buttons if (btn.callback_data or "").startswith("mktexplain:")]
        self.assertEqual(len(explain), 1)
        self.assertEqual(explain[0].callback_data, "mktexplain:42")
        self.assertIn("📖", explain[0].text)

    def test_explain_button_in_dedicated_row(self):
        """Глоссарий-кнопка в отдельном ряду — не перемешана с управляющими.
        Это даёт ей визуальный вес и не ломает «Лучшая | Обновить | 🔔»."""
        kb = self._kb(is_enabled=True, current="crypto", user_id=42)
        # Найдём ряд содержащий mktexplain
        explain_row = None
        for row in kb.inline_keyboard:
            if any((btn.callback_data or "").startswith("mktexplain:") for btn in row):
                explain_row = row
                break
        self.assertIsNotNone(explain_row)
        self.assertEqual(len(explain_row), 1, "explain-button должна быть одна в своём ряду")

    def test_uid_isolation(self):
        kb_a = self._kb(is_enabled=False, current="summary", user_id=100)
        kb_b = self._kb(is_enabled=False, current="summary", user_id=200)
        cd_a = next(
            (btn.callback_data for row in kb_a.inline_keyboard for btn in row
             if (btn.callback_data or "").startswith("mktexplain:")), None
        )
        cd_b = next(
            (btn.callback_data for row in kb_b.inline_keyboard for btn in row
             if (btn.callback_data or "").startswith("mktexplain:")), None
        )
        self.assertNotEqual(cd_a, cd_b)


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestMarketsGlossaryText(unittest.TestCase):
    """Глоссарий покрывает все термины + влезает в Telegram-лимит + безопасен MD V1."""

    @classmethod
    def setUpClass(cls):
        from main import _markets_glossary_text  # noqa: PLC0415

        cls._txt = staticmethod(_markets_glossary_text)

    def test_returns_non_empty(self):
        self.assertIsInstance(self._txt(), str)
        self.assertGreater(len(self._txt()), 1000)  # достаточный объём

    def test_fits_telegram_4096_limit(self):
        self.assertLess(len(self._txt()), 4096)

    def test_explains_sr_levels(self):
        """Главное что добавили — S/R. Без этого глоссарий бесполезен."""
        text = self._txt()
        # Должно явно объяснять Resistance / Support.
        self.assertIn("R", text)
        self.assertIn("S", text)
        self.assertTrue(
            any(s in text for s in ("Сопротивление", "сопротивлен"))
        )
        self.assertTrue(
            any(s in text for s in ("Поддержка", "поддержк"))
        )
        # Также должно быть про confluence (MA + S/R) и свинг-Nд.
        self.assertIn("свинг", text)
        self.assertTrue(
            any(s in text for s in ("confluence", "MA200", "MA50"))
        )

    def test_explains_ma_triggers(self):
        text = self._txt()
        self.assertIn("MA50", text)
        self.assertIn("MA200", text)
        self.assertIn("▲", text)
        self.assertIn("▼", text)

    def test_explains_sl_tp(self):
        text = self._txt()
        self.assertIn("TP", text)
        self.assertIn("SL", text)
        self.assertIn("R/R", text)

    def test_explains_quant_components(self):
        text = self._txt()
        # σ̂, Hurst, VR, Markov, score — основные стат-метрики.
        self.assertIn("σ̂", text)
        self.assertTrue(any(s in text for s in ("Hurst", " H ")))
        self.assertIn("VR", text)
        self.assertIn("Markov", text)
        self.assertTrue(
            any(s in text for s in ("score", "Score", "рейтинг"))
        )

    def test_explains_trend_complexity(self):
        text = self._txt()
        self.assertIn("UPTREND", text)
        self.assertIn("DOWNTREND", text)
        self.assertIn("SIDEWAYS", text)
        self.assertTrue(
            any(s in text for s in ("TRENDING", "MEAN-REVERTING", "RANDOM"))
        )

    def test_no_bare_underscores_outside_code(self):
        """MD V1 трактует `_` как italic. Все `_` вне backtick-code-span'ов
        должны быть экранированы `\\_` — иначе TelegramBadRequest
        при нечётном количестве."""
        import re  # noqa: PLC0415

        text = self._txt()
        no_code = re.sub(r"`[^`]*`", "", text)
        bare = [
            i
            for i, ch in enumerate(no_code)
            if ch == "_" and (i == 0 or no_code[i - 1] != "\\")
        ]
        self.assertEqual(
            bare,
            [],
            f"Found {len(bare)} bare '_' outside code spans — "
            f"Telegram MD V1 will fail to parse",
        )


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestMarketsExplainCallback(unittest.TestCase):
    """UID-guard + отправка глоссария в чат."""

    @classmethod
    def setUpClass(cls):
        from main import handle_markets_explain_callback  # noqa: PLC0415

        cls._handler = staticmethod(handle_markets_explain_callback)

    def _make_callback(self, *, data: str, from_user_id: int, chat_id: int = 999):
        cb = MagicMock()
        cb.data = data
        cb.answer = AsyncMock()
        cb.from_user = MagicMock()
        cb.from_user.id = from_user_id
        cb.message = MagicMock()
        cb.message.chat = MagicMock()
        cb.message.chat.id = chat_id
        return cb

    def test_uid_mismatch_alerts(self):
        cb = self._make_callback(data="mktexplain:42", from_user_id=99)
        with patch("main.bot", new=MagicMock(send_message=AsyncMock())) as bot_mock:
            asyncio.run(self._handler(cb))
            cb.answer.assert_awaited_with(
                "Кнопка не с твоего аккаунта", show_alert=True
            )
            bot_mock.send_message.assert_not_awaited()

    def test_uid_match_sends_glossary(self):
        cb = self._make_callback(data="mktexplain:42", from_user_id=42)
        with patch("main.bot", new=MagicMock(send_message=AsyncMock())) as bot_mock:
            asyncio.run(self._handler(cb))
            cb.answer.assert_awaited()
            bot_mock.send_message.assert_awaited_once()
            args, kwargs = bot_mock.send_message.call_args
            self.assertEqual(args[0], 999)  # chat_id
            self.assertEqual(kwargs.get("parse_mode"), "Markdown")
            body = args[1]
            # Должно содержать ключевые S/R-термины.
            self.assertTrue(
                any(s in body for s in ("Сопротивление", "сопротивлен"))
            )
            self.assertIn("σ̂", body)

    def test_malformed_callback_no_crash(self):
        cb = self._make_callback(data="mktexplain:notanint", from_user_id=42)
        with patch("main.bot", new=MagicMock(send_message=AsyncMock())) as bot_mock:
            asyncio.run(self._handler(cb))
            cb.answer.assert_awaited()
            bot_mock.send_message.assert_not_awaited()

    def test_empty_callback_no_crash(self):
        cb = self._make_callback(data="", from_user_id=42)
        with patch("main.bot", new=MagicMock(send_message=AsyncMock())) as bot_mock:
            asyncio.run(self._handler(cb))
            cb.answer.assert_awaited()
            bot_mock.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
