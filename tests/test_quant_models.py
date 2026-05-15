"""Regression tests for the new quant models.

Covers:
- core.market_complexity.variance_ratio_test (Lo-MacKinlay)
- core.market_complexity.permutation_entropy (Bandt-Pompe)
- core.markov_regime.analyze_markov_regime
- core.volatility_forecast.forecast_volatility_ewma

Plus end-to-end rendering via web_search._quant_lines for crypto and macro
synthetic time series.

Strategy: synthetic returns with known properties (random walk vs persistent
AR(1) vs mean-reverting). Не используем numpy/scipy чтобы тесты были
быстрые и без heavy deps на CI.
"""
from __future__ import annotations

import math
import random
import unittest


def _gen_random_walk(n: int, seed: int = 42) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(0.0, 0.02) for _ in range(n)]


def _gen_persistent(n: int, ar: float = 0.5, seed: int = 42) -> list[float]:
    """AR(1) returns: r_t = ar*r_{t-1} + eps."""
    rng = random.Random(seed)
    out = []
    prev = 0.0
    for _ in range(n):
        r = ar * prev + rng.gauss(0.001, 0.015)
        out.append(r)
        prev = r
    return out


def _gen_mean_reverting(n: int, ar: float = -0.5, seed: int = 42) -> list[float]:
    rng = random.Random(seed)
    out = []
    prev = 0.0
    for _ in range(n):
        r = ar * prev + rng.gauss(0.0, 0.015)
        out.append(r)
        prev = r
    return out


def _closes_from_returns(returns: list[float], start: float = 100.0) -> list[float]:
    closes = [start]
    for r in returns:
        closes.append(closes[-1] * math.exp(r))
    return closes


# ── Variance Ratio Test ─────────────────────────────────────────────────────


class TestVarianceRatioTest(unittest.TestCase):

    def test_random_walk_not_rejected(self):
        from core.market_complexity import variance_ratio_test

        returns = _gen_random_walk(300, seed=1)
        vrt = variance_ratio_test(returns, k=2)
        self.assertIsNotNone(vrt)
        self.assertTrue(
            vrt.random_walk,
            f"Random walk should NOT be rejected. VR={vrt.vr:.3f}, z={vrt.z_stat:.2f}",
        )
        self.assertAlmostEqual(vrt.vr, 1.0, delta=0.20)

    def test_persistent_series_rejects_random_walk(self):
        from core.market_complexity import variance_ratio_test

        returns = _gen_persistent(300, ar=0.5, seed=2)
        vrt = variance_ratio_test(returns, k=2)
        self.assertIsNotNone(vrt)
        self.assertFalse(
            vrt.random_walk,
            f"Persistent AR(0.5) should reject H0. VR={vrt.vr:.3f}, z={vrt.z_stat:.2f}",
        )
        # AR(0.5) → VR(2) >= 1.0 + 0.5 = 1.5 in theory; expect > 1.2 empirically
        self.assertGreater(vrt.vr, 1.2)
        self.assertGreater(vrt.z_stat, 1.96)

    def test_mean_reverting_rejects_random_walk(self):
        from core.market_complexity import variance_ratio_test

        returns = _gen_mean_reverting(300, ar=-0.5, seed=3)
        vrt = variance_ratio_test(returns, k=2)
        self.assertIsNotNone(vrt)
        self.assertFalse(vrt.random_walk)
        # AR(-0.5) → VR(2) < 1.0
        self.assertLess(vrt.vr, 0.8)
        self.assertLess(vrt.z_stat, -1.96)

    def test_short_series_returns_none(self):
        from core.market_complexity import variance_ratio_test

        self.assertIsNone(variance_ratio_test([0.01] * 5, k=2))
        self.assertIsNone(variance_ratio_test([], k=2))

    def test_invalid_k_returns_none(self):
        from core.market_complexity import variance_ratio_test

        returns = _gen_random_walk(200, seed=4)
        self.assertIsNone(variance_ratio_test(returns, k=1))
        self.assertIsNone(variance_ratio_test(returns, k=0))

    def test_p_value_in_valid_range(self):
        from core.market_complexity import variance_ratio_test

        returns = _gen_random_walk(300, seed=5)
        vrt = variance_ratio_test(returns, k=2)
        self.assertIsNotNone(vrt)
        self.assertGreaterEqual(vrt.p_value, 0.0)
        self.assertLessEqual(vrt.p_value, 1.0)


# ── Permutation Entropy ─────────────────────────────────────────────────────


class TestPermutationEntropy(unittest.TestCase):

    def test_random_walk_near_max_entropy(self):
        from core.market_complexity import permutation_entropy

        returns = _gen_random_walk(300, seed=1)
        pe = permutation_entropy(returns, order=3)
        self.assertIsNotNone(pe)
        # Random walk → uniform distribution over 6 permutations → PE → 1.0.
        self.assertGreater(pe, 0.97)
        self.assertLessEqual(pe, 1.0)

    def test_monotonic_series_low_entropy(self):
        from core.market_complexity import permutation_entropy

        # Strict monotonic increase → only one permutation pattern (0,1,2) → PE=0
        returns = [float(i) for i in range(200)]
        pe = permutation_entropy(returns, order=3)
        self.assertIsNotNone(pe)
        self.assertLess(pe, 0.1)

    def test_short_series_returns_none(self):
        from core.market_complexity import permutation_entropy

        self.assertIsNone(permutation_entropy([0.01, 0.02], order=3))
        self.assertIsNone(permutation_entropy([], order=3))

    def test_invalid_params_returns_none(self):
        from core.market_complexity import permutation_entropy

        returns = _gen_random_walk(200)
        self.assertIsNone(permutation_entropy(returns, order=1))
        self.assertIsNone(permutation_entropy(returns, order=3, delay=0))

    def test_normalized_in_unit_range(self):
        from core.market_complexity import permutation_entropy

        returns = _gen_random_walk(300, seed=6)
        pe = permutation_entropy(returns, order=3)
        self.assertGreaterEqual(pe, 0.0)
        self.assertLessEqual(pe, 1.0)


# ── Markov Regime ───────────────────────────────────────────────────────────


class TestMarkovRegime(unittest.TestCase):

    def test_returns_result_for_sufficient_history(self):
        from core.markov_regime import analyze_markov_regime, STATE_NAMES

        returns = _gen_random_walk(200, seed=1)
        mk = analyze_markov_regime(returns)
        self.assertIsNotNone(mk)
        self.assertIn(mk.current_state, STATE_NAMES)
        # Transition matrix rows must sum to 1
        for row in mk.transition_matrix:
            self.assertAlmostEqual(sum(row), 1.0, places=6)
        # Stationary distribution sums to 1
        self.assertAlmostEqual(sum(mk.stationary_distribution.values()), 1.0, places=4)

    def test_short_series_returns_none(self):
        from core.markov_regime import analyze_markov_regime

        self.assertIsNone(analyze_markov_regime([0.01] * 10))
        self.assertIsNone(analyze_markov_regime([]))

    def test_persistent_series_high_dwell(self):
        from core.markov_regime import analyze_markov_regime

        # Strong positive autocorrelation → UP/DOWN tend to repeat
        returns = _gen_persistent(300, ar=0.7, seed=10)
        mk = analyze_markov_regime(returns)
        self.assertIsNotNone(mk)
        max_dwell = max(mk.expected_dwell_bars.values())
        # Persistent series typically dwell > 2 bars on average for some state
        self.assertGreater(max_dwell, 1.8)

    def test_next_step_probs_sum_to_one(self):
        from core.markov_regime import analyze_markov_regime

        returns = _gen_random_walk(200, seed=2)
        mk = analyze_markov_regime(returns)
        self.assertIsNotNone(mk)
        self.assertAlmostEqual(sum(mk.next_step_probs.values()), 1.0, places=4)

    def test_quantile_thresholds_ordered(self):
        from core.markov_regime import analyze_markov_regime

        returns = _gen_random_walk(200, seed=3)
        mk = analyze_markov_regime(returns)
        self.assertIsNotNone(mk)
        q_low, q_high = mk.quantile_thresholds
        self.assertLessEqual(q_low, q_high)


# ── EWMA Volatility Forecast ────────────────────────────────────────────────


class TestVolatilityForecast(unittest.TestCase):

    def test_returns_forecast_for_sufficient_history(self):
        from core.volatility_forecast import forecast_volatility_ewma

        returns = _gen_random_walk(200, seed=1)
        vf = forecast_volatility_ewma(returns)
        self.assertIsNotNone(vf)
        self.assertGreater(vf.sigma_1d, 0)
        self.assertGreater(vf.sigma_1d_pct, 0)
        self.assertGreater(vf.sigma_annualized_pct, 0)

    def test_short_series_returns_none(self):
        from core.volatility_forecast import forecast_volatility_ewma

        self.assertIsNone(forecast_volatility_ewma([0.01] * 10))
        self.assertIsNone(forecast_volatility_ewma([]))

    def test_invalid_decay_returns_none(self):
        from core.volatility_forecast import forecast_volatility_ewma

        returns = _gen_random_walk(200)
        self.assertIsNone(forecast_volatility_ewma(returns, decay=0.0))
        self.assertIsNone(forecast_volatility_ewma(returns, decay=1.0))
        self.assertIsNone(forecast_volatility_ewma(returns, decay=-0.5))

    def test_annualization_consistent(self):
        from core.volatility_forecast import forecast_volatility_ewma

        returns = _gen_random_walk(200, seed=4)
        vf = forecast_volatility_ewma(returns, annualization_days=365)
        self.assertIsNotNone(vf)
        expected = vf.sigma_1d_pct * math.sqrt(365.0)
        self.assertAlmostEqual(vf.sigma_annualized_pct, expected, places=4)

    def test_vol_responds_to_recent_shock(self):
        from core.volatility_forecast import forecast_volatility_ewma

        # Quiet baseline (small noise) + huge spike at the end → forecast
        # should be elevated. Need genuine noise so warm-up variance is > 0.
        rng = random.Random(99)
        quiet = [rng.gauss(0.0, 0.001) for _ in range(150)]
        with_spike = list(quiet) + [0.10 for _ in range(5)]
        vf_quiet = forecast_volatility_ewma(quiet)
        vf_spike = forecast_volatility_ewma(with_spike)
        self.assertIsNotNone(vf_quiet)
        self.assertIsNotNone(vf_spike)
        self.assertGreater(vf_spike.sigma_1d, vf_quiet.sigma_1d * 2.0)


# ── End-to-end rendering via web_search._quant_lines (compact 2-line format) ──


class TestQuantLinesRendering(unittest.TestCase):

    def _full_fields(self, returns: list[float]) -> dict:
        from web_search import _compute_complexity_fields

        closes = _closes_from_returns(returns)
        return _compute_complexity_fields(closes)

    def test_quant_lines_for_random_walk_at_most_two_lines(self):
        from web_search import _quant_lines

        fields = self._full_fields(_gen_random_walk(220, seed=1))
        lines = _quant_lines(fields)
        # Compact format: вердикт + Markov — максимум 2 строки.
        self.assertLessEqual(len(lines), 2)
        joined = "\n".join(lines)
        # Verdict line carries Hurst (H=) and one of the regime labels.
        self.assertIn("H=", joined)
        # Markov line is always last and prefixed by 🎲 Markov.
        self.assertIn("Markov", joined)

    def test_quant_lines_for_persistent_includes_h0_rejected(self):
        from web_search import _quant_lines

        fields = self._full_fields(_gen_persistent(220, ar=0.5, seed=2))
        lines = _quant_lines(fields)
        joined = "\n".join(lines)
        # Persistent series should reject H0 — фолдится в verdict-строку.
        self.assertIn("H0 отвергнут", joined)

    def test_quant_lines_skip_when_no_data(self):
        from web_search import _quant_lines

        # No complexity-related fields at all → no lines.
        self.assertEqual(_quant_lines({"price": 100.0}), [])

    def test_markov_line_compact_format(self):
        from web_search import _markov_line

        line = _markov_line({
            "markov_state": "UP",
            "markov_next_probs": {"UP": 0.5, "FLAT": 0.3, "DOWN": 0.2},
            "markov_dwell_bars": 2.5,
        })
        self.assertIsNotNone(line)
        # Compact form: "Markov UP (~2.5 баров)  UP 50% / FLAT 30% / DOWN 20%"
        self.assertIn("Markov UP", line)
        self.assertIn("UP 50%", line)
        self.assertIn("FLAT 30%", line)
        self.assertIn("DOWN 20%", line)
        self.assertIn("2.5 баров", line)
        # Legacy verbose markers should be gone.
        self.assertNotIn("МАРКОВ:", line)
        self.assertNotIn("текущее=", line)
        self.assertNotIn("next:", line)

    def test_markov_line_skips_when_missing(self):
        from web_search import _markov_line

        self.assertIsNone(_markov_line({}))
        self.assertIsNone(_markov_line({"markov_state": "UP"}))  # no probs

    def test_verdict_line_carries_vrt_and_vol(self):
        from web_search import _complexity_line

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
        self.assertIsNotNone(line)
        # Verdict line is a single string carrying all 5 categories of info.
        self.assertIn("ТРЕНД", line)
        self.assertIn("H=0.58", line)
        self.assertIn("PE=0.99", line)
        self.assertIn("VR=1.42", line)
        self.assertIn("H0 отвергнут", line)
        self.assertIn("σ̂=1.84%", line)
        self.assertIn("год.35%", line)

    def test_verdict_line_warns_on_random_walk_with_low_score(self):
        from web_search import _complexity_line

        line = _complexity_line({
            "complexity_hint": "RANDOM_WALK",
            "hurst": 0.50,
            "perm_entropy": 0.99,
            "tradeable_score": 0.22,
            "vrt_ratio": 0.96,
            "vrt_random_walk": True,
            "vol_sigma_1d_pct": 2.30,
            "vol_sigma_annual_pct": 44.0,
        })
        self.assertIsNotNone(line)
        self.assertIn("RANDOM WALK", line)
        self.assertIn("⚠️ untradeable", line)
        self.assertIn("H0 не отвергнут", line)
        self.assertIn("σ̂=2.30%", line)


if __name__ == "__main__":
    unittest.main()
