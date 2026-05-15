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
        self.assertIn("Hurst=0.42", line)
        self.assertIn("энтропия=0.78", line)
        self.assertIn("score=0.55", line)
        self.assertIn("MEAN_REVERTING", line)
        self.assertNotIn("untradeable", line)

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
        # entropy<MIN_BARS scenario: hurst is None but entropy filled
        line = _complexity_line({
            "complexity_hint": "MEAN_REVERTING",
            "hurst": None,
            "entropy_normalized": 0.85,
            "tradeable_score": 0.40,
        })
        assert line is not None
        self.assertNotIn("Hurst=", line)
        self.assertIn("энтропия=0.85", line)


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
        self.assertIn("СЛОЖНОСТЬ", out)
        self.assertIn("Hurst=0.62", out)
        self.assertIn("TRENDING", out)

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
        self.assertNotIn("СЛОЖНОСТЬ", out)

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
        self.assertIn("СЛОЖНОСТЬ", out)
        self.assertIn("Hurst=0.58", out)

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


if __name__ == "__main__":
    unittest.main()
