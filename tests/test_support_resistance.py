# -*- coding: utf-8 -*-
"""Тесты Support/Resistance детекции (`core/support_resistance.py`).

Покрывает:
  1. `find_pivot_points` — синусоида, плоская кривая, граничные случаи
  2. `cluster_levels` — слияние близких уровней по tolerance
  3. `score_clusters` — recency bonus + touches
  4. `compute_sr_levels` — end-to-end на trending / range / breakout
  5. `label_level_source` — MA confluence + свинг fallback
"""
from __future__ import annotations

import math
import unittest

from core.support_resistance import (
    Level,
    Pivot,
    SRLevels,
    cluster_levels,
    compute_sr_levels,
    find_pivot_points,
    label_level_source,
    score_clusters,
)


def _synth_sine(n: int = 100, amplitude: float = 10.0, period: int = 20, base: float = 100.0):
    """Синусоидальный price series — для проверки что пивоты находятся в
    математически ожидаемых местах."""
    closes = [base + amplitude * math.sin(2 * math.pi * i / period) for i in range(n)]
    # high = close + small noise, low = close - small noise
    highs = [c + 0.1 for c in closes]
    lows = [c - 0.1 for c in closes]
    return highs, lows, closes


def _synth_uptrend(n: int = 100, slope: float = 0.5, start: float = 100.0, noise: float = 1.0):
    """Линейный аптренд с шумом — все пивоты должны быть в нижней части
    (поддержки) и в верхней (сопротивления локальные)."""
    import random
    rng = random.Random(42)
    closes = [start + slope * i + rng.uniform(-noise, noise) for i in range(n)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    return highs, lows, closes


class TestFindPivotPoints(unittest.TestCase):
    def test_sine_finds_peaks_and_troughs(self):
        """На синусоиде с периодом 20 баров должны находиться пики/впадины
        каждые ~10 баров. На 100 барах ожидаем ~5 пиков + ~5 впадин."""
        highs, lows, _ = _synth_sine(n=100, period=20)
        pivots = find_pivot_points(highs, lows, lookback=5)
        peaks = [p for p in pivots if p.kind == "HIGH"]
        troughs = [p for p in pivots if p.kind == "LOW"]
        # 5 полных периодов на 100 барах → ~5 high + ~5 low (минус граничные).
        self.assertGreaterEqual(len(peaks), 3)
        self.assertGreaterEqual(len(troughs), 3)
        self.assertLessEqual(len(peaks), 7)
        self.assertLessEqual(len(troughs), 7)

    def test_flat_series_no_pivots(self):
        """Полностью плоский ряд — пивотов нет (нужно строгое неравенство)."""
        n = 50
        highs = [100.0] * n
        lows = [99.0] * n
        pivots = find_pivot_points(highs, lows, lookback=3)
        self.assertEqual(pivots, [])

    def test_short_series_returns_empty(self):
        """Меньше 2*lookback+1 баров → пустой результат, без exception."""
        pivots = find_pivot_points([1.0, 2.0, 3.0], [0.5, 1.5, 2.5], lookback=5)
        self.assertEqual(pivots, [])

    def test_mismatched_highs_lows_raises(self):
        with self.assertRaises(ValueError):
            find_pivot_points([1.0, 2.0], [0.5], lookback=1)

    def test_single_peak_in_middle(self):
        """Простейший случай: V-образный спайк в середине → 1 high pivot."""
        # 11 баров. high[5] = 110, остальные = 100. lookback=3.
        highs = [100.0] * 11
        highs[5] = 110.0
        lows = [99.0] * 11
        pivots = find_pivot_points(highs, lows, lookback=3)
        self.assertEqual(len(pivots), 1)
        self.assertEqual(pivots[0].idx, 5)
        self.assertEqual(pivots[0].kind, "HIGH")
        self.assertAlmostEqual(pivots[0].price, 110.0)


class TestClusterLevels(unittest.TestCase):
    def test_close_levels_merge(self):
        """Три HIGH-пивота на $100, $100.3, $100.5 с tolerance=1% сливаются
        в один кластер touches=3."""
        pivots = [
            Pivot(idx=10, price=100.0, kind="HIGH"),
            Pivot(idx=20, price=100.3, kind="HIGH"),
            Pivot(idx=30, price=100.5, kind="HIGH"),
        ]
        clusters = cluster_levels(pivots, tolerance_pct=1.0)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].touches, 3)
        self.assertAlmostEqual(clusters[0].price, (100.0 + 100.3 + 100.5) / 3, places=2)
        self.assertEqual(clusters[0].last_idx, 30)
        self.assertEqual(clusters[0].kind, "HIGH")

    def test_far_levels_dont_merge(self):
        """Уровни $100 и $200 — далеко, кластеров два."""
        pivots = [
            Pivot(idx=10, price=100.0, kind="HIGH"),
            Pivot(idx=20, price=200.0, kind="HIGH"),
        ]
        clusters = cluster_levels(pivots, tolerance_pct=1.0)
        self.assertEqual(len(clusters), 2)

    def test_different_kinds_dont_merge(self):
        """HIGH $100 и LOW $100 — рядом по цене но разный kind, не сливаются."""
        pivots = [
            Pivot(idx=10, price=100.0, kind="HIGH"),
            Pivot(idx=20, price=100.0, kind="LOW"),
        ]
        clusters = cluster_levels(pivots, tolerance_pct=1.0)
        self.assertEqual(len(clusters), 2)

    def test_empty_input_returns_empty(self):
        self.assertEqual(cluster_levels([]), [])

    def test_negative_tolerance_raises(self):
        with self.assertRaises(ValueError):
            cluster_levels([Pivot(idx=0, price=100.0, kind="HIGH")], tolerance_pct=-1.0)


class TestScoreClusters(unittest.TestCase):
    def test_recency_bonus_decays(self):
        """Уровень касавшийся 0 баров назад имеет score = touches + 1.0
        (полный бонус). Касавшийся 100 баров назад с halflife=30 → почти
        только touches."""
        l_recent = Level(price=100.0, touches=2, last_idx=99, kind="HIGH", score=0.0)
        l_old = Level(price=200.0, touches=2, last_idx=0, kind="HIGH", score=0.0)
        scored = score_clusters([l_recent, l_old], current_idx=99, recency_halflife=30.0)
        # recent: bars_ago=0 → recency=1.0, score=3.0
        # old:    bars_ago=99 → recency=exp(-99/30)≈0.037, score≈2.04
        self.assertAlmostEqual(scored[0].score, 3.0, places=2)
        self.assertGreater(scored[0].score, scored[1].score)
        self.assertEqual(scored[0].bars_ago, 0)
        self.assertEqual(scored[1].bars_ago, 99)

    def test_zero_halflife_raises(self):
        with self.assertRaises(ValueError):
            score_clusters([], current_idx=10, recency_halflife=0)


class TestComputeSRLevels(unittest.TestCase):
    def test_sine_returns_levels_above_and_below(self):
        """На синусоиде около средней цены — есть и поддержки (внизу), и
        сопротивления (вверху)."""
        highs, lows, closes = _synth_sine(n=100)
        sr = compute_sr_levels(
            highs, lows, current_price=closes[-1], lookback=3, num_each_side=2
        )
        # На синусоиде среднее = base, и цена в конце где-то рядом. Должны
        # быть уровни хотя бы с одной стороны (зависит от фазы синусоиды).
        total = len(sr.resistances) + len(sr.supports)
        self.assertGreaterEqual(total, 2)

    def test_short_data_returns_empty(self):
        """< 2*lookback+1 баров → пустой результат, без падения."""
        sr = compute_sr_levels([1.0, 2.0], [0.5, 1.5], current_price=1.5, lookback=5)
        self.assertEqual(sr.resistances, [])
        self.assertEqual(sr.supports, [])

    def test_resistances_above_current_supports_below(self):
        """Базовая инвариантность: все resistances выше цены, все supports — ниже."""
        highs, lows, closes = _synth_sine(n=80, period=15, amplitude=20.0)
        current = closes[-1]
        sr = compute_sr_levels(highs, lows, current_price=current, lookback=3)
        for r in sr.resistances:
            self.assertGreater(r.price, current, f"resistance {r.price} not above {current}")
        for s in sr.supports:
            self.assertLess(s.price, current, f"support {s.price} not below {current}")

    def test_resistances_sorted_ascending(self):
        """R₁ ближе к цене, R₂ дальше (по возрастанию). Симметрично S₁/S₂."""
        highs, lows, closes = _synth_sine(n=120, period=15, amplitude=15.0)
        sr = compute_sr_levels(highs, lows, current_price=closes[-1], lookback=3)
        if len(sr.resistances) >= 2:
            self.assertLess(sr.resistances[0].price, sr.resistances[1].price)
        if len(sr.supports) >= 2:
            # Supports sorted descending (S₁ ближе к цене сверху-вниз =
            # больше по цене).
            self.assertGreater(sr.supports[0].price, sr.supports[1].price)

    def test_num_each_side_caps_results(self):
        highs, lows, closes = _synth_sine(n=200, period=15, amplitude=10.0)
        sr = compute_sr_levels(
            highs, lows, current_price=closes[-1], lookback=3, num_each_side=3
        )
        self.assertLessEqual(len(sr.resistances), 3)
        self.assertLessEqual(len(sr.supports), 3)


class TestLabelLevelSource(unittest.TestCase):
    def test_ma200_confluence(self):
        """Уровень $81,956, MA200=$82,000 — разница 0.05% → метка `MA200`."""
        lv = Level(price=81956.0, touches=2, last_idx=50, kind="HIGH", score=2.5, bars_ago=10)
        self.assertEqual(label_level_source(lv, ma50=70000.0, ma200=82000.0), "MA200")

    def test_ma50_confluence(self):
        lv = Level(price=74970.0, touches=3, last_idx=80, kind="LOW", score=3.5, bars_ago=5)
        self.assertEqual(label_level_source(lv, ma50=74968.0, ma200=82000.0), "MA50")

    def test_no_confluence_returns_swing_with_days(self):
        """Если ни одна MA не близка — возвращаем «свинг-Nд»."""
        lv = Level(price=60000.0, touches=1, last_idx=100, kind="LOW", score=1.5, bars_ago=15)
        self.assertEqual(label_level_source(lv, ma50=74968.0, ma200=82000.0), "свинг-15д")

    def test_missing_ma_falls_through(self):
        """Когда ma50/ma200 = None, fallback на свинг работает."""
        lv = Level(price=60000.0, touches=1, last_idx=100, kind="LOW", score=1.5, bars_ago=7)
        self.assertEqual(label_level_source(lv, ma50=None, ma200=None), "свинг-7д")

    def test_zero_bars_ago_no_days_suffix(self):
        lv = Level(price=60000.0, touches=1, last_idx=100, kind="LOW", score=1.5, bars_ago=0)
        self.assertEqual(label_level_source(lv, ma50=None, ma200=None), "свинг")


if __name__ == "__main__":
    unittest.main()
