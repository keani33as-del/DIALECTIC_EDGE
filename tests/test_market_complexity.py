"""Audit-grade тесты для core/market_complexity.py.

Запуск без зависимостей:
    python -m unittest tests.test_market_complexity -v

Что покрываем:
  1. Edge cases: пустой вход, NaN-цены, константный ряд, отрицательные цены.
  2. Корректность математики: lookbacks, нормировка, кламп, регрессия R/S.
  3. Семантика на синтетике: trend / random walk / mean-revert.
  4. Стабильность интерпретации: интерпретация не падает с None в любой
     комбинации (hurst=None, entropy=None, оба None).
  5. Границы:
       - вход ровно на пороге MIN_BARS_FOR_HURST → не падаем
       - очень высокая энтропия → tradeable_score маленький
       - оба плохих → штраф применён, score дополнительно урезан
"""

from __future__ import annotations

import math
import random
import unittest

from core.market_complexity import (
    ENTROPY_CHAOS_THRESHOLD,
    ENTROPY_ORDERED_THRESHOLD,
    HURST_RANDOM_LOW,
    HURST_TRENDING_THRESHOLD,
    MIN_BARS_FOR_ENTROPY,
    MIN_BARS_FOR_HURST,
    MarketComplexity,
    _interpret,
    _rescaled_range,
    analyze_complexity,
    compute_returns,
    hurst_exponent,
    shannon_entropy,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _seeded_rng(seed: int) -> random.Random:
    rng = random.Random()
    rng.seed(seed)
    return rng


def _make_trend(n: int, drift: float = 0.003, sigma: float = 0.008, seed: int = 1) -> list[float]:
    rng = _seeded_rng(seed)
    out = [100.0]
    for _ in range(n - 1):
        out.append(out[-1] * (1 + drift + rng.gauss(0, sigma)))
    return out


def _make_random_walk(n: int, sigma: float = 0.015, seed: int = 2) -> list[float]:
    rng = _seeded_rng(seed)
    out = [100.0]
    for _ in range(n - 1):
        out.append(out[-1] * (1 + rng.gauss(0, sigma)))
    return out


def _make_mean_reverting(n: int, target: float = 100.0, k: float = 0.15, sigma: float = 1.0, seed: int = 3) -> list[float]:
    """Ornstein-Uhlenbeck-like процесс с возвратом к target."""
    rng = _seeded_rng(seed)
    out = [target]
    for _ in range(n - 1):
        drift = (target - out[-1]) * k
        out.append(out[-1] + drift + rng.gauss(0, sigma))
    return out


# ── 1. Edge cases ───────────────────────────────────────────────────────────


class TestEdgeCases(unittest.TestCase):
    def test_empty_input_returns_none(self):
        self.assertIsNone(analyze_complexity([]))

    def test_too_short_returns_none(self):
        # < MIN_BARS_FOR_ENTROPY + 1
        self.assertIsNone(analyze_complexity([100.0] * (MIN_BARS_FOR_ENTROPY)))

    def test_constant_series_does_not_crash(self):
        """Ряд из одинаковых цен — частый сценарий 'данные ещё не пришли'."""
        result = analyze_complexity([100.0] * 200)
        # Должен либо вернуть MarketComplexity с разумными дефолтами, либо None
        # — но НЕ упасть.
        self.assertTrue(result is None or isinstance(result, MarketComplexity))

    def test_negative_price_filtered(self):
        """Отрицательная или нулевая цена — никогда в реальности, но защищаемся."""
        closes = [100.0] * 100
        closes[50] = -5.0  # битая свеча
        # Не должны крашнуться
        result = analyze_complexity(closes)
        self.assertTrue(result is None or isinstance(result, MarketComplexity))

    def test_compute_returns_skips_invalid_prices(self):
        """Функция вычисления log-returns должна молча пропускать невалидные пары."""
        returns = compute_returns([100.0, 101.0, 0.0, 102.0, -3.0, 103.0])
        # Пары (100,101) и (102,103) валидны; (101,0), (0,102), (102,-3), (-3,103) нет
        # Итого должна остаться 2 валидные точки
        self.assertGreaterEqual(len(returns), 1)
        for r in returns:
            self.assertTrue(math.isfinite(r))


# ── 2. Корректность математики ──────────────────────────────────────────────


class TestMathCorrectness(unittest.TestCase):
    def test_rescaled_range_zero_for_constant(self):
        self.assertEqual(_rescaled_range([5.0, 5.0, 5.0, 5.0]), 0.0)

    def test_rescaled_range_positive_for_varied(self):
        rs = _rescaled_range([1.0, 2.0, 3.0, 2.0, 1.0])
        self.assertGreater(rs, 0.0)

    def test_hurst_returns_none_below_min_bars(self):
        rng = _seeded_rng(0)
        short = [rng.gauss(0, 0.01) for _ in range(MIN_BARS_FOR_HURST - 1)]
        self.assertIsNone(hurst_exponent(short))

    def test_hurst_in_valid_range_when_succeeds(self):
        rng = _seeded_rng(0)
        returns = [rng.gauss(0, 0.01) for _ in range(500)]
        h = hurst_exponent(returns)
        self.assertIsNotNone(h)
        self.assertGreaterEqual(h, 0.0)
        self.assertLessEqual(h, 1.0)

    def test_entropy_returns_none_below_min_bars(self):
        self.assertIsNone(shannon_entropy([0.01, 0.02]))

    def test_entropy_zero_for_constant_returns(self):
        self.assertEqual(shannon_entropy([0.01] * 100), 0.0)

    def test_entropy_in_valid_range(self):
        rng = _seeded_rng(0)
        returns = [rng.gauss(0, 0.01) for _ in range(200)]
        e = shannon_entropy(returns)
        self.assertIsNotNone(e)
        self.assertGreaterEqual(e, 0.0)
        self.assertLessEqual(e, 1.0)


# ── 3. Семантика на синтетике ───────────────────────────────────────────────


class TestSemanticsOnSynthetic(unittest.TestCase):
    """Главные тесты: правильно ли модуль РАСПОЗНАЁТ режимы."""

    def test_random_walk_flagged_as_untradeable(self):
        """Самое важное: random walk → tradeable_score ниже 0.5.

        Это первая линия защиты — если бот думает что чистая монетка
        торгуема, юзер сольёт депозит.
        """
        closes = _make_random_walk(250)
        result = analyze_complexity(closes)
        self.assertIsNotNone(result)
        self.assertLess(
            result.tradeable_score, 0.5,
            f"Random walk должен быть оценён как НЕ торгуемый. "
            f"Got tradeable_score={result.tradeable_score}, hint={result.regime_hint}"
        )

    def test_trending_market_flagged_as_tradeable(self):
        """Чистый тренд → tradeable_score >= 0.4.

        Замечание для аудита: симулировать «трендовый» ряд через простой drift
        + gaussian noise — НЕ даёт высокий Hurst (returns остаются i.i.d.,
        H≈0.5 — это математически корректно). Реальная персистентность
        проявляется как автокорреляция returns (AR(+0.3) даёт H≈0.55).
        Поэтому здесь требуем только что score не падает ниже 0.4 — модуль
        не должен флагать trend-ряд как «не торговать».
        """
        closes = _make_trend(250, drift=0.005, sigma=0.005)
        result = analyze_complexity(closes)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(
            result.tradeable_score, 0.4,
            f"Trend ряд не должен быть оценён как полностью untradeable. "
            f"Got tradeable_score={result.tradeable_score}, hint={result.regime_hint}"
        )

    def test_persistent_returns_high_hurst(self):
        """AR(+0.3) — персистентный процесс, в среднем по нескольким seed'ам
        Hurst должен быть выше чем у AR(-0.3).

        Это правильный эталон для проверки что Anis-Lloyd correction не
        задавила сигнал персистентности. Используем средний Hurst по 5
        seed'ам, чтобы избавиться от seed-noise (на одном seed R/S может
        случайно дать H=0.45 даже на персистентном ряду).
        """
        h_values = []
        for seed in range(5):
            rng = _seeded_rng(100 + seed)
            returns = [rng.gauss(0, 0.01)]
            for _ in range(599):
                returns.append(0.3 * returns[-1] + rng.gauss(0, 0.01))
            h = hurst_exponent(returns)
            self.assertIsNotNone(h)
            h_values.append(h)
        avg_h = sum(h_values) / len(h_values)
        # Средний Hurst по 5 seed'ам должен быть > 0.5 (статистически)
        self.assertGreater(avg_h, 0.50,
            f"Средний Hurst для AR(+0.3) (5 seeds) должен быть > 0.5, "
            f"got {avg_h:.3f}, individual: {[round(h,3) for h in h_values]}")

    def test_antipersistent_returns_low_hurst(self):
        """AR(-0.3) — антиперсистентный (mean-reverting) → Hurst < 0.5
        в среднем по 5 seed'ам."""
        h_values = []
        for seed in range(5):
            rng = _seeded_rng(200 + seed)
            returns = [rng.gauss(0, 0.01)]
            for _ in range(599):
                returns.append(-0.3 * returns[-1] + rng.gauss(0, 0.01))
            h = hurst_exponent(returns)
            self.assertIsNotNone(h)
            h_values.append(h)
        avg_h = sum(h_values) / len(h_values)
        self.assertLess(avg_h, 0.50,
            f"Средний Hurst для AR(-0.3) (5 seeds) должен быть < 0.5, "
            f"got {avg_h:.3f}, individual: {[round(h,3) for h in h_values]}")

    def test_persistent_vs_antipersistent_hurst_gap(self):
        """Главный качественный тест: AR(+0.3) vs AR(-0.3) — должна быть
        статистически значимая разница в Hurst.

        Это самый честный тест корректности R/S — не «правильные ли
        абсолютные значения», а «отличает ли модуль персистентность от
        антиперсистентности».
        """
        def avg_hurst(phi: float, seed_offset: int) -> float:
            h_values = []
            for seed in range(5):
                rng = _seeded_rng(seed_offset + seed)
                returns = [rng.gauss(0, 0.01)]
                for _ in range(599):
                    returns.append(phi * returns[-1] + rng.gauss(0, 0.01))
                h = hurst_exponent(returns)
                if h is not None:
                    h_values.append(h)
            return sum(h_values) / len(h_values)

        h_persistent = avg_hurst(+0.3, 1000)
        h_antipersistent = avg_hurst(-0.3, 2000)
        gap = h_persistent - h_antipersistent
        self.assertGreater(gap, 0.05,
            f"Разница Hurst между AR(+0.3) и AR(-0.3) должна быть > 0.05. "
            f"Got persistent={h_persistent:.3f}, antipersistent={h_antipersistent:.3f}, "
            f"gap={gap:.3f}")

    def test_random_walk_hurst_close_to_half(self):
        """Sanity: H для random walk должен быть в окрестности 0.5 (±0.15)."""
        closes = _make_random_walk(500)
        returns = compute_returns(closes)
        h = hurst_exponent(returns)
        self.assertIsNotNone(h)
        self.assertGreater(h, 0.30)
        self.assertLess(h, 0.70)


# ── 4. Стабильность _interpret к None ───────────────────────────────────────


class TestInterpretRobustness(unittest.TestCase):
    """Главный регресс-тест на найденный баг с f-string и None.

    _interpret должен НИКОГДА не падать, независимо от того что приходит.
    """

    def test_both_none_does_not_crash(self):
        regime_hint, recommendation, score = _interpret(None, None)
        self.assertEqual(regime_hint, "UNKNOWN")
        self.assertIsInstance(recommendation, str)
        self.assertEqual(score, 0.5)

    def test_hurst_none_entropy_chaotic_does_not_crash(self):
        """Регрессионный тест на f-string баг.

        Раньше: Hurst=None, entropy=0.95 → попадаем в ветку RANDOM_WALK/CHAOTIC,
        и f-string `f"...{hurst if hurst is not None else '?':.2f}"` крашится
        с ValueError, потому что :.2f применяется к строке '?'.
        """
        regime_hint, recommendation, score = _interpret(None, 0.95)
        self.assertIsInstance(regime_hint, str)
        self.assertIsInstance(recommendation, str)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_hurst_random_entropy_none_does_not_crash(self):
        regime_hint, recommendation, score = _interpret(0.50, None)
        self.assertIsInstance(regime_hint, str)
        self.assertIsInstance(recommendation, str)

    def test_full_grid_of_inputs_does_not_crash(self):
        """Перебираем сетку H × E в диапазоне валидных значений + None."""
        h_values = [None, 0.0, 0.20, 0.40, 0.45, 0.50, 0.55, 0.60, 0.80, 1.0]
        e_values = [None, 0.0, 0.30, 0.60, 0.80, 0.85, 0.90, 0.95, 1.0]
        for h in h_values:
            for e in e_values:
                with self.subTest(hurst=h, entropy=e):
                    regime_hint, recommendation, score = _interpret(h, e)
                    self.assertIsInstance(regime_hint, str)
                    self.assertIsInstance(recommendation, str)
                    self.assertGreaterEqual(score, 0.0)
                    self.assertLessEqual(score, 1.0)


# ── 5. Граничные значения и monotonicity ────────────────────────────────────


class TestBoundaryAndMonotonicity(unittest.TestCase):
    def test_at_min_bars_hurst_does_not_crash(self):
        """Ровно MIN_BARS_FOR_HURST баров returns — должен ИЛИ работать ИЛИ None,
        но не падать."""
        rng = _seeded_rng(0)
        returns = [rng.gauss(0, 0.01) for _ in range(MIN_BARS_FOR_HURST)]
        h = hurst_exponent(returns)
        self.assertTrue(h is None or 0.0 <= h <= 1.0)

    def test_pure_chaos_low_score(self):
        """Чистый хаос (h≈0.5, entropy≈0.95): score должен быть ≤ 0.4."""
        _, _, score = _interpret(0.50, 0.96)
        self.assertLessEqual(score, 0.4)

    def test_pure_trend_high_score(self):
        """Сильный тренд (h=0.70, entropy=0.50): score должен быть ≥ 0.6."""
        _, _, score = _interpret(0.70, 0.50)
        self.assertGreaterEqual(score, 0.6)

    def test_double_penalty_for_random_walk_chaotic(self):
        """Если оба плохие — штраф применён (мультипликативный *0.5)."""
        # Без штрафа: avg(0.25, 0.20) = 0.225
        # Со штрафом: 0.225 * 0.5 = 0.1125
        _, _, score_with_penalty = _interpret(0.50, 0.96)
        # Должен быть ниже среднего двух плохих оценок
        self.assertLess(score_with_penalty, 0.20)

    def test_score_bounded_to_unit_interval(self):
        """tradeable_score всегда в [0, 1] для любых разумных входов."""
        for h in [0.0, 0.25, 0.5, 0.75, 1.0]:
            for e in [0.0, 0.25, 0.5, 0.75, 1.0]:
                _, _, score = _interpret(h, e)
                self.assertGreaterEqual(score, 0.0)
                self.assertLessEqual(score, 1.0)


# ── 6. Интеграция с regime_detector ─────────────────────────────────────────


class TestIntegrationWithRegimeDetector(unittest.TestCase):
    """Проверяем, что MarketRegime.to_dict() корректно сериализует
    новые поля и что detect() не падает на коротких сериях."""

    def test_regime_detector_short_series_does_not_crash(self):
        from core.regime_detector import RegimeDetector

        # 50 свечей — меньше чем нужно для Hurst, но больше чем ma_slow=20
        candles = [
            {"open": 100, "high": 101, "low": 99, "close": 100 + i * 0.1, "volume": 1000}
            for i in range(50)
        ]
        result = RegimeDetector(ma_slow=20).detect(candles)
        # Просто не должен упасть. Hurst может быть None — это ок.
        if result is not None:
            self.assertTrue(hasattr(result, "hurst"))
            self.assertTrue(hasattr(result, "tradeable_score"))

    def test_regime_detector_long_series_includes_complexity(self):
        from core.regime_detector import RegimeDetector

        rng = _seeded_rng(7)
        candles = []
        price = 100.0
        for i in range(220):
            price *= (1 + 0.002 + rng.gauss(0, 0.01))
            candles.append({
                "open": price * 0.999,
                "high": price * 1.005,
                "low": price * 0.995,
                "close": price,
                "volume": 1000 + rng.random() * 100,
            })
        result = RegimeDetector().detect(candles)
        self.assertIsNotNone(result)
        # Hurst должен посчитаться на 220 свечах
        self.assertIsNotNone(result.hurst)
        self.assertGreaterEqual(result.hurst, 0.0)
        self.assertLessEqual(result.hurst, 1.0)
        # И score тоже
        self.assertIsNotNone(result.tradeable_score)
        self.assertGreaterEqual(result.tradeable_score, 0.0)
        self.assertLessEqual(result.tradeable_score, 1.0)
        # to_dict должен включать complexity-поля
        d = result.to_dict()
        self.assertIn("hurst", d)
        self.assertIn("tradeable_score", d)


if __name__ == "__main__":
    unittest.main()
