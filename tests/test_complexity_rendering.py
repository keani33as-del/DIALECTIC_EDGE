# -*- coding: utf-8 -*-
"""Regression-tests for Hurst+entropy wiring in web_search.py.

Covers:
    1. `_compute_complexity_fields` round-trips analyze_complexity output into
       the per-asset dict shape used by the rest of the codebase.
    2. `_complexity_line` returns None on missing/unknown hint and renders the
       expected one-liner shape otherwise (including the ⚠️ untradeable mark).
    3. `format_prices_for_agents` actually surfaces the complexity line for
       both crypto and macro assets when fields are present — this is what
       makes the data visible to /markets *and* the Bull/Bear/Synth context.
"""
from __future__ import annotations

import math
import random
import unittest

from web_search import (
    _compute_complexity_fields,
    _complexity_line,
    _trigger_lines,
    format_prices_for_agents,
)


def _random_walk(n: int = 250, seed: int = 42) -> list[float]:
    rng = random.Random(seed)
    p = 100.0
    out = [p]
    for _ in range(n):
        p *= math.exp(rng.gauss(0, 0.02))
        out.append(p)
    return out


def _strong_trend(n: int = 250, seed: int = 7) -> list[float]:
    """Series with a clearly positive drift relative to noise — Hurst should
    land in the trending zone after Anis-Lloyd correction."""
    rng = random.Random(seed)
    p = 100.0
    out = [p]
    for _ in range(n):
        p *= math.exp(0.003 + rng.gauss(0, 0.005))
        out.append(p)
    return out


class TestComputeComplexityFields(unittest.TestCase):
    def test_returns_dict_for_sufficient_history(self):
        fields = _compute_complexity_fields(_random_walk())
        self.assertIn("hurst", fields)
        self.assertIn("entropy_normalized", fields)
        self.assertIn("tradeable_score", fields)
        self.assertIn("complexity_hint", fields)
        # Score is always rounded to 3 decimals downstream
        self.assertIsInstance(fields["tradeable_score"], float)
        self.assertGreaterEqual(fields["tradeable_score"], 0.0)
        self.assertLessEqual(fields["tradeable_score"], 1.0)

    def test_short_series_returns_empty(self):
        # Below MIN_BARS_FOR_ENTROPY+1 = 33 → analyze_complexity returns None
        self.assertEqual(_compute_complexity_fields([1.0, 2.0, 3.0]), {})

    def test_empty_input_returns_empty(self):
        self.assertEqual(_compute_complexity_fields([]), {})

    def test_random_walk_not_classified_trending(self):
        # The audit fix shipped in 0077e5d guarantees random walks aren't
        # labeled TRENDING. Re-asserting here so future refactors don't
        # silently regress that classification.
        fields = _compute_complexity_fields(_random_walk())
        self.assertNotEqual(fields.get("complexity_hint"), "TRENDING")


class TestComplexityLine(unittest.TestCase):
    def test_returns_none_without_hint(self):
        self.assertIsNone(_complexity_line({}))
        self.assertIsNone(_complexity_line({"complexity_hint": None}))

    def test_returns_none_for_unknown(self):
        self.assertIsNone(
            _complexity_line(
                {"complexity_hint": "UNKNOWN", "hurst": 0.5, "tradeable_score": 0.5}
            )
        )

    def test_renders_standard_shape(self):
        line = _complexity_line({
            "complexity_hint": "MEAN_REVERTING",
            "hurst": 0.42,
            "entropy_normalized": 0.78,
            "tradeable_score": 0.55,
        })
        assert line is not None
        self.assertIn("H=0.42", line)
        self.assertIn("score=0.55", line)
        self.assertIn("MEAN-REVERTING", line)
        self.assertNotIn("untradeable", line)
        # Compact format uses an emoji-leading verdict, not the legacy header
        self.assertNotIn("СЛОЖНОСТЬ", line)

    def test_warns_when_score_below_threshold(self):
        line = _complexity_line({
            "complexity_hint": "RANDOM_WALK",
            "hurst": 0.50,
            "entropy_normalized": 0.92,
            "tradeable_score": 0.25,
        })
        assert line is not None
        self.assertIn("⚠️", line)
        self.assertIn("untradeable", line)

    def test_handles_partial_fields(self):
        # entropy<MIN_BARS scenario: hurst is None but entropy filled. Renderer
        # falls back to legacy 'entropy_normalized' label when perm_entropy
        # is also missing.
        line = _complexity_line({
            "complexity_hint": "MEAN_REVERTING",
            "hurst": None,
            "entropy_normalized": 0.85,
            "tradeable_score": 0.40,
        })
        assert line is not None
        self.assertNotIn("H=", line)
        self.assertIn("энтр=0.85", line)
        self.assertIn("score=0.40", line)

    def test_renders_vrt_and_vol_when_present(self):
        # New compact format folds VRT and EWMA σ-forecast into the same line.
        line = _complexity_line({
            "complexity_hint": "TRENDING",
            "hurst": 0.58,
            "perm_entropy": 0.99,
            "tradeable_score": 0.65,
            "vrt_ratio": 1.42,
            "vrt_random_walk": False,
            "vol_sigma_1d_pct": 1.84,
            "vol_sigma_annual_pct": 35.2,
        })
        assert line is not None
        self.assertIn("ТРЕНД", line)
        self.assertIn("PE=0.99", line)
        self.assertIn("VR=1.42", line)
        self.assertIn("H0 отвергнут", line)
        self.assertIn("σ̂=1.84%", line)
        self.assertIn("год.35%", line)


class TestFormatPricesForAgentsRendersComplexity(unittest.TestCase):
    def test_crypto_asset_block_includes_complexity_line(self):
        prices = {
            "BTC": {
                "price": 100000.0,
                "change_24h": 1.5,
                "source": "Binance",
                "trend": "UPTREND",
                "trend_emoji": "📈",
                "ma50": 95000.0,
                "ma200": 80000.0,
                "above_ma50": True,
                "above_ma200": True,
                "complexity_hint": "TRENDING",
                "hurst": 0.62,
                "entropy_normalized": 0.74,
                "tradeable_score": 0.82,
            }
        }
        out = format_prices_for_agents(prices)
        self.assertIn("Bitcoin (BTC)", out)
        # Compact verdict line replaces the old "СЛОЖНОСТЬ" header.
        self.assertIn("ТРЕНД", out)
        self.assertIn("H=0.62", out)

    def test_crypto_without_complexity_renders_without_line(self):
        prices = {
            "ETH": {
                "price": 4000.0,
                "change_24h": -0.3,
                "source": "Binance",
                "trend": "SIDEWAYS",
                "trend_emoji": "↔️",
            }
        }
        out = format_prices_for_agents(prices)
        self.assertIn("Ethereum (ETH)", out)
        # Без complexity-полей verdict-строка пропускается полностью.
        # Базовая "ТРЕНД:"-строка от MA50/MA200 — отдельная, она остаётся.
        # Проверяем уникальный маркер verdict-строки: "H=" (Hurst) появляется
        # ТОЛЬКО в нашей quant-сводке и нигде больше.
        self.assertNotIn("H=", out)
        self.assertNotIn("Markov", out)

    def test_macro_index_includes_complexity_line(self):
        prices = {
            "SPX": {
                "price": 6000.0,
                "change_24h": 0.4,
                "source": "Yahoo",
                "trend": "UPTREND",
                "trend_emoji": "📈",
                "ma50": 5800.0,
                "ma200": 5500.0,
                "above_ma50": True,
                "above_ma200": True,
                "complexity_hint": "TRENDING",
                "hurst": 0.58,
                "entropy_normalized": 0.76,
                "tradeable_score": 0.71,
            }
        }
        out = format_prices_for_agents(prices)
        self.assertIn("S&P 500", out)
        self.assertIn("ТРЕНД", out)
        self.assertIn("H=0.58", out)

    def test_random_walk_renders_untradeable_warning(self):
        # End-to-end: a real synthetic random walk → analyze_complexity →
        # _compute_complexity_fields → format_prices_for_agents emits the
        # untradeable warning so agents can read it.
        fields = _compute_complexity_fields(_random_walk())
        if not fields:
            self.skipTest("complexity fields unavailable in this environment")
        # Force the warning by capping score below threshold (audit safeguard
        # is permissive on synthetic data — we test the renderer, not the math)
        fields = dict(fields)
        fields["tradeable_score"] = 0.25
        fields["complexity_hint"] = "RANDOM_WALK"
        prices = {
            "BTC": {
                "price": 100000.0,
                "change_24h": 0.1,
                "source": "Binance",
                "trend": "SIDEWAYS",
                "trend_emoji": "↔️",
                **fields,
            }
        }
        out = format_prices_for_agents(prices)
        self.assertIn("⚠️", out)
        self.assertIn("untradeable", out)


class TestTriggerLines(unittest.TestCase):
    """LONG/SHORT MA-trigger renderer.

    Контракт `_trigger_lines`:
      • При наличии обеих MA → две строки `▲ выше … → LONG` и
        `▼ ниже … → SHORT`.
      • Верхний уровень = max(MA50, MA200), нижний = min(MA50, MA200) —
        логика идентична `build_short_report` в main.py.
      • При отсутствии MA → пустой список (graceful degradation для
        активов с коротким рядом).
      • Префикс `$` опционален — индексы (SPX/NDX/DXY/VIX) рендерим
        без него, а сырьё (OIL/GOLD) — с ним.
    """

    def test_empty_when_no_ma(self):
        self.assertEqual(_trigger_lines({}), [])
        self.assertEqual(_trigger_lines({"ma50": 100.0}), [])
        self.assertEqual(_trigger_lines({"ma200": 200.0}), [])

    def test_upper_picks_max_lower_picks_min(self):
        # BTC SIDEWAYS: цена между MA50 ($74,969) и MA200 ($81,957) →
        # верхний триггер должен быть MA200 (выше), нижний MA50 (ниже).
        lines = _trigger_lines({"ma50": 74969.0, "ma200": 81957.0})
        self.assertEqual(len(lines), 2)
        self.assertIn("▲ выше", lines[0])
        self.assertIn("$81,957", lines[0])
        self.assertIn("(MA200)", lines[0])
        self.assertIn("→ LONG", lines[0])
        self.assertIn("▼ ниже", lines[1])
        self.assertIn("$74,969", lines[1])
        self.assertIn("(MA50)", lines[1])
        self.assertIn("→ SHORT", lines[1])

    def test_uptrend_ma50_above_ma200(self):
        # SPX UPTREND: MA50 > MA200 → верхний это MA50, нижний — MA200.
        lines = _trigger_lines({"ma50": 6910.0, "ma200": 6775.0}, prefix="")
        self.assertEqual(len(lines), 2)
        self.assertIn("6,910", lines[0])
        self.assertIn("(MA50)", lines[0])
        self.assertIn("→ LONG", lines[0])
        self.assertIn("6,775", lines[1])
        self.assertIn("(MA200)", lines[1])
        self.assertIn("→ SHORT", lines[1])

    def test_no_dollar_prefix_when_disabled(self):
        # SPX/NDX/VIX/DXY — индексы, без $.
        lines = _trigger_lines({"ma50": 6910.0, "ma200": 6775.0}, prefix="")
        self.assertNotIn("$", lines[0])
        self.assertNotIn("$", lines[1])

    def test_dollar_prefix_default(self):
        lines = _trigger_lines({"ma50": 74969.0, "ma200": 81957.0})
        self.assertIn("$", lines[0])
        self.assertIn("$", lines[1])

    def test_xrp_low_price_precision(self):
        # XRP MA50=$1.30, MA200=$1.65 → раньше `_fmt_money` без adaptive
        # precision рендерил их как $1 / $2. Здесь проверяем что мы выдаем
        # `$1.30` / `$1.65` (две цифры после точки сохраняются).
        lines = _trigger_lines({"ma50": 1.30, "ma200": 1.65})
        self.assertIn("$1.65", lines[0])
        self.assertIn("$1.30", lines[1])

    def test_indent_default_is_four_spaces(self):
        lines = _trigger_lines({"ma50": 100.0, "ma200": 200.0})
        self.assertTrue(lines[0].startswith("    ▲"))
        self.assertTrue(lines[1].startswith("    ▼"))

    def test_format_prices_renders_triggers_for_crypto(self):
        # Smoke-тест: убеждаемся что в полном выводе live-контекста
        # для крипты появляются обе MA-trigger строки.
        prices = {
            "BTC": {
                "price": 79199.0,
                "change_24h": -2.8,
                "source": "Binance",
                "trend": "SIDEWAYS",
                "trend_emoji": "↔️",
                "ma50": 74969.0,
                "ma200": 81957.0,
                "above_ma50": True,
                "above_ma200": False,
            }
        }
        out = format_prices_for_agents(prices)
        # Две строки триггеров — обе должны быть в выводе.
        self.assertIn("▲ выше $81,957 (MA200) → LONG", out)
        self.assertIn("▼ ниже $74,969 (MA50) → SHORT", out)
        # И они должны идти ДО строки тренда (визуально первыми).
        idx_long = out.index("▲ выше")
        idx_trend = out.index("ТРЕНД:")
        self.assertLess(idx_long, idx_trend)

    def test_format_prices_renders_triggers_for_macro(self):
        prices = {
            "SPX": {
                "price": 7501.0,
                "change_24h": 0.3,
                "source": "Yahoo",
                "trend": "UPTREND",
                "trend_emoji": "📈",
                "ma50": 6910.0,
                "ma200": 6775.0,
                "above_ma50": True,
                "above_ma200": True,
            }
        }
        out = format_prices_for_agents(prices)
        # SPX без $-префикса.
        self.assertIn("▲ выше 6,910 (MA50) → LONG", out)
        self.assertIn("▼ ниже 6,775 (MA200) → SHORT", out)

    def test_format_prices_renders_triggers_for_commodity(self):
        prices = {
            "OIL_WTI": {
                "price": 103.0,
                "change_24h": 1.5,
                "source": "Yahoo",
                "trend": "UPTREND",
                "trend_emoji": "📈",
                "ma50": 97.22,
                "ma200": 70.63,
                "above_ma50": True,
                "above_ma200": True,
            }
        }
        out = format_prices_for_agents(prices)
        # WTI рендерится с $-префиксом.
        self.assertIn("▲ выше $97.22 (MA50) → LONG", out)
        self.assertIn("▼ ниже $70.63 (MA200) → SHORT", out)

    def test_format_prices_skips_triggers_when_ma_missing(self):
        prices = {
            "ETH": {
                "price": 4000.0,
                "change_24h": -0.3,
                "source": "Binance",
                # ma50/ma200 отсутствуют — нет триггеров
            }
        }
        out = format_prices_for_agents(prices)
        self.assertNotIn("▲ выше", out)
        self.assertNotIn("▼ ниже", out)


if __name__ == "__main__":
    unittest.main()
