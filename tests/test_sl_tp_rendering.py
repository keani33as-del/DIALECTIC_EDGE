# -*- coding: utf-8 -*-
"""Regression-tests для SL/TP блока в `/markets`.

Покрывают:
  1. `_sl_tp_lines` рендерит две строки (LONG + SHORT) c корректными
     уровнями `price × (1 ± k·σ̂)` и tick-rounding.
  2. Graceful-degradation: нет σ̂ / нет цены / нулевая σ̂ → пусто.
  3. `format_prices_for_agents` пропускает блок через интеграционно —
     строка реально видна в выводе `/markets`.
  4. Tick-size для XRP — 0.1 (Bybit Spot), для BTC — 0.01.
  5. R/R = 2:1 (TP_dist = 2·SL_dist в абсолюте).
  6. Помощь `/help markets` ссылается на новую SL/TP-строку.

Эти тесты — фундамент: они описывают «что должен видеть юзер в
`/markets` при наличии σ̂». Любая регрессия формата или формулы их
сломает до проверки на пользователе.
"""

from __future__ import annotations

import unittest

from web_search import _fmt_pct, _sl_tp_lines, format_prices_for_agents


class TestFmtPct(unittest.TestCase):
    """`_fmt_pct(value)` — печатает `+5.0%` / `−2.5%` (U+2212 минус)."""

    def test_positive_uses_plus(self):
        self.assertEqual(_fmt_pct(4.95), "+5.0%")

    def test_negative_uses_unicode_minus(self):
        # Юникодный минус (U+2212), а не ASCII '-' — Markdown будет
        # ровно выровнен с `−2.18%` из строки изменений.
        self.assertEqual(_fmt_pct(-2.475), "−2.5%")
        self.assertNotIn("-", _fmt_pct(-2.475))


class TestSlTpLines(unittest.TestCase):
    """`_sl_tp_lines(p, asset)` — LONG/SHORT уровни от σ̂."""

    def test_empty_when_no_sigma(self):
        self.assertEqual(_sl_tp_lines({"price": 100.0}, "BTC"), [])

    def test_empty_when_zero_sigma(self):
        self.assertEqual(
            _sl_tp_lines({"price": 100.0, "vol_sigma_1d_pct": 0.0}, "BTC"),
            [],
        )

    def test_empty_when_negative_sigma(self):
        # Защита от мусорных данных — отрицательная σ̂ не имеет смысла.
        self.assertEqual(
            _sl_tp_lines({"price": 100.0, "vol_sigma_1d_pct": -1.0}, "BTC"),
            [],
        )

    def test_empty_when_no_price(self):
        self.assertEqual(_sl_tp_lines({"vol_sigma_1d_pct": 1.5}, "BTC"), [])

    def test_empty_when_zero_price(self):
        self.assertEqual(
            _sl_tp_lines({"price": 0.0, "vol_sigma_1d_pct": 1.5}, "BTC"),
            [],
        )

    def test_btc_renders_two_lines(self):
        # BTC: price=79,118, σ̂=1.65% → SL=1.5×=2.475%, TP=3×=4.95%
        # LONG SL  = 79118 × (1-0.02475) = 77,159.83 → tick 0.01 → 77,159.83
        # LONG TP  = 79118 × (1+0.0495)  = 83,034.34 → 83,034.34
        # SHORT TP = 79118 × (1-0.0495)  = 75,201.66
        # SHORT SL = 79118 × (1+0.02475) = 81,076.17
        lines = _sl_tp_lines({"price": 79118.0, "vol_sigma_1d_pct": 1.65}, "BTC")
        self.assertEqual(len(lines), 2)
        long_line, short_line = lines

        self.assertIn("LONG", long_line)
        self.assertIn("TP $83,034", long_line)
        self.assertIn("SL $77,160", long_line)
        self.assertIn("(+5.0%)", long_line)
        # Юникодный минус (U+2212), не ASCII '-'
        self.assertIn("(−2.5%)", long_line)
        self.assertIn("R/R 2:1", long_line)

        self.assertIn("SHORT", short_line)
        self.assertIn("TP $75,202", short_line)
        self.assertIn("SL $81,076", short_line)
        self.assertIn("(−5.0%)", short_line)
        self.assertIn("(+2.5%)", short_line)
        self.assertIn("R/R 2:1", short_line)

    def test_xrp_tick_rounding_to_one_decimal(self):
        # XRP на Bybit Spot — 1 знак после точки. Без округления у нас
        # вылетают ордера вида $1.4567 — Bybit reject.
        # price=1.46, σ̂=2.5% → SL=3.75%, TP=7.5%
        # LONG TP = 1.46 × 1.075 = 1.5695 → 1.6 (tick=0.1)
        # LONG SL = 1.46 × 0.9625 = 1.4053 → 1.4
        # SHORT TP = 1.46 × 0.925 = 1.3505 → 1.4
        # SHORT SL = 1.46 × 1.0375 = 1.5148 → 1.5
        lines = _sl_tp_lines({"price": 1.46, "vol_sigma_1d_pct": 2.5}, "XRP")
        self.assertEqual(len(lines), 2)
        long_line, short_line = lines
        # Точные значения — кратные 0.1 (1.4 / 1.5 / 1.6) после tick-rounding.
        # `_fmt_money` сохраняет 2 знака для цен ≤ $10 — это норм, главное,
        # что вторая цифра — всегда `0`.
        self.assertIn("$1.60", long_line)
        self.assertIn("$1.40", long_line)
        self.assertIn("$1.40", short_line)
        self.assertIn("$1.50", short_line)

    def test_rr_ratio_is_two(self):
        # Геометрия: TP_dist = 2 × SL_dist для R/R = 2:1.
        # Проверяем через парсинг % из строки.
        lines = _sl_tp_lines({"price": 100.0, "vol_sigma_1d_pct": 2.0}, "BTC")
        # σ̂=2% → SL=3%, TP=6%
        self.assertIn("(+6.0%)", lines[0])
        self.assertIn("(−3.0%)", lines[0])
        self.assertIn("(−6.0%)", lines[1])
        self.assertIn("(+3.0%)", lines[1])

    def test_unknown_asset_falls_back_to_default_tick(self):
        # Актив не в `ASSET_TICK_SIZE` (например, DOGE) — fallback 0.0001.
        lines = _sl_tp_lines(
            {"price": 0.15, "vol_sigma_1d_pct": 3.0},
            "DOGE",
        )
        # Не падаем, рендерим 2 строки. Точное значение не важно — важно
        # что не падаем и формат сохраняется.
        self.assertEqual(len(lines), 2)
        self.assertIn("LONG", lines[0])
        self.assertIn("SHORT", lines[1])

    def test_indent_is_four_spaces_by_default(self):
        lines = _sl_tp_lines({"price": 100.0, "vol_sigma_1d_pct": 2.0}, "BTC")
        for line in lines:
            self.assertTrue(line.startswith("    🎯"))


class TestFormatPricesRendersSlTp(unittest.TestCase):
    """Интеграционно: `/markets` показывает SL/TP блок."""

    def _btc(self) -> dict:
        # Минимальный валидный prices-dict для BTC с σ̂.
        return {
            "BTC": {
                "price": 79118.0,
                "change_24h": -2.18,
                "change_7d": -1.9,
                "change_30d": 5.3,
                "source": "Binance",
                "trend": "SIDEWAYS",
                "trend_emoji": "↔️",
                "ma50": 75222.0,
                "ma200": 81788.0,
                "above_ma50": True,
                "above_ma200": False,
                "complexity_hint": "MEAN_REVERTING",
                "hurst": 0.45,
                "tradeable_score": 0.49,
                "vol_sigma_1d_pct": 1.65,
                "vol_sigma_annual_pct": 32.0,
            }
        }

    def test_crypto_block_includes_long_and_short_sl_tp(self):
        out = format_prices_for_agents(self._btc())
        self.assertIn("🎯 LONG", out)
        self.assertIn("🎯 SHORT", out)
        self.assertIn("R/R 2:1", out)
        # Точные числа от прод-кейса (79,118 × 1.65%).
        self.assertIn("TP $83,034", out)
        self.assertIn("SL $77,160", out)

    def test_sl_tp_follows_ma_triggers(self):
        # Визуальный порядок: цена → MA-триггеры → SL/TP → тренд → quant.
        # Это специально — юзер сначала видит «при пробое», потом «если
        # входим сейчас», потом контекст.
        out = format_prices_for_agents(self._btc())
        idx_triggers = out.index("▲ выше")
        idx_sltp = out.index("🎯 LONG")
        idx_trend = out.index("ТРЕНД:")
        self.assertLess(idx_triggers, idx_sltp)
        self.assertLess(idx_sltp, idx_trend)

    def test_sl_tp_skipped_when_no_sigma(self):
        # Короткий ряд — нет σ̂ → SL/TP блок просто пропадает (не падаем).
        prices = self._btc()
        prices["BTC"].pop("vol_sigma_1d_pct")
        prices["BTC"].pop("vol_sigma_annual_pct")
        out = format_prices_for_agents(prices)
        # Базовая строка с ценой и MA-триггеры — есть.
        self.assertIn("Bitcoin (BTC)", out)
        self.assertIn("▲ выше", out)
        # А блока SL/TP — нет.
        self.assertNotIn("🎯 LONG", out)
        self.assertNotIn("🎯 SHORT", out)

    def test_works_for_all_crypto_symbols(self):
        # Все 5 крипты-символов получают SL/TP при наличии σ̂.
        prices = {}
        for sym in ("BTC", "ETH", "SOL", "BNB", "XRP"):
            prices[sym] = {
                "price": 100.0,
                "change_24h": 0.0,
                "source": "Binance",
                "trend": "SIDEWAYS",
                "trend_emoji": "↔️",
                "vol_sigma_1d_pct": 2.0,
            }
        out = format_prices_for_agents(prices)
        # 5 LONG + 5 SHORT = 10 строк с 🎯
        self.assertEqual(out.count("🎯 LONG"), 5)
        self.assertEqual(out.count("🎯 SHORT"), 5)


class TestHelpDocumentsSlTp(unittest.TestCase):
    """`/help markets` теперь упоминает SL/TP блок — без шпаргалки юзер
    смотрит на новые цифры и не понимает что это."""

    def test_help_mentions_sl_tp_section(self):
        from main import _markets_help_text

        text = _markets_help_text()
        # Заголовок секции по новой нумерации.
        self.assertIn("SL / TP от текущей цены", text)
        # Формула, чтобы юзер мог считать сам.
        self.assertIn("1.5", text)
        self.assertIn("σ̂", text)
        self.assertIn("R/R", text)
        # Telegram-лимит 4096 символов — справка должна укладываться.
        self.assertLessEqual(len(text), 4096)


if __name__ == "__main__":
    unittest.main()
