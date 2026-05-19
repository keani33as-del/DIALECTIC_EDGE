"""Unit-тесты для core/recalibration.py.

Покрывают:
  * `IsotonicCalibrator.fit` — корректность PAV-алгоритма:
    - монотонность сохраняется,
    - tied-x теперь пулируются (раньше был баг с (x_tied, 0/1) сортировкой),
    - violations корректно объединяются,
    - synthetic overconfident модель: 0.9 → ~realized 0.5.
  * `IsotonicCalibrator.predict` — clip [0,1], бинарный поиск bin'а,
    behaviour на пустом калибраторе.
  * `IsotonicCalibrator.to_dict/from_dict` — roundtrip без потери.
  * `brier_score`, `hit_rate`, `score_to_probability` — utility-функции.
  * `fit_isotonic_and_evaluate` — IS-метрики и improvement.
"""

from __future__ import annotations

import random
import unittest

from core.recalibration import (
    IsotonicCalibrator,
    brier_score,
    fit_isotonic_and_evaluate,
    hit_rate,
    score_to_probability,
)


class ScoreToProbabilityTestCase(unittest.TestCase):
    def test_none_returns_half(self) -> None:
        self.assertEqual(score_to_probability(None), 0.5)

    def test_zero_clipped_to_half(self) -> None:
        # Score < 50 не должен выдавать сделок, но защита clip'ит p>=0.5.
        self.assertEqual(score_to_probability(0), 0.5)
        self.assertEqual(score_to_probability(25), 0.5)

    def test_fifty_is_half(self) -> None:
        self.assertEqual(score_to_probability(50), 0.5)

    def test_hundred_is_one(self) -> None:
        self.assertEqual(score_to_probability(100), 1.0)

    def test_seventy_five(self) -> None:
        self.assertEqual(score_to_probability(75), 0.75)

    def test_overflow_clipped(self) -> None:
        self.assertEqual(score_to_probability(200), 1.0)


class BrierScoreTestCase(unittest.TestCase):
    def test_perfect_predictions(self) -> None:
        # p=1 на wins, p=0 на losses → Brier 0
        self.assertEqual(brier_score([1.0, 0.0, 1.0], [True, False, True]), 0.0)

    def test_worst_predictions(self) -> None:
        # p=0 на wins, p=1 на losses → Brier 1
        self.assertEqual(brier_score([0.0, 1.0, 0.0], [True, False, True]), 1.0)

    def test_coin_flip(self) -> None:
        # p=0.5 всегда → Brier 0.25
        self.assertEqual(brier_score([0.5, 0.5, 0.5], [True, False, True]), 0.25)

    def test_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            brier_score([], [])

    def test_length_mismatch_raises(self) -> None:
        with self.assertRaises(ValueError):
            brier_score([0.5, 0.7], [True])


class HitRateTestCase(unittest.TestCase):
    def test_all_above_threshold_all_win(self) -> None:
        self.assertEqual(hit_rate([0.7, 0.8, 0.9], [True, True, True]), 1.0)

    def test_all_above_threshold_half_win(self) -> None:
        self.assertEqual(
            hit_rate([0.6, 0.6, 0.6, 0.6], [True, True, False, False]),
            0.5,
        )

    def test_none_above_threshold(self) -> None:
        # Все p <= threshold → нет «выстрелов» → 0
        self.assertEqual(hit_rate([0.4, 0.5, 0.5], [True, True, True]), 0.0)

    def test_custom_threshold(self) -> None:
        self.assertEqual(
            hit_rate([0.6, 0.7, 0.8], [True, True, False], threshold=0.65),
            0.5,  # 0.7 win, 0.8 loss → 1/2
        )


class IsotonicCalibratorFitTestCase(unittest.TestCase):
    def test_monotonic_clean_data(self) -> None:
        """Уже монотонные данные → калибратор не должен ничего ломать."""
        probs = [0.1, 0.3, 0.5, 0.7, 0.9]
        wins = [False, False, True, True, True]
        cal = IsotonicCalibrator().fit(probs, wins)
        # Каждый predict монотонно неубывающий.
        ys = [cal.predict(p) for p in probs]
        for i in range(len(ys) - 1):
            self.assertLessEqual(ys[i], ys[i + 1])

    def test_pav_pools_violations(self) -> None:
        """Сильное нарушение монотонности должно пулироваться."""
        probs = [0.1, 0.2, 0.3, 0.4]
        wins = [True, True, False, False]  # 1, 1, 0, 0 — должен слиться в один блок 0.5
        cal = IsotonicCalibrator().fit(probs, wins)
        # На любом x в [0.1, 0.4] калибратор должен говорить mean = 0.5.
        for p in probs:
            self.assertAlmostEqual(cal.predict(p), 0.5)

    def test_tied_x_pools_realized_mean(self) -> None:
        """100 точек с p=0.9, win-rate 50% → калибратор должен выдавать ~0.5."""
        random.seed(42)
        probs = [0.9] * 100
        wins = [random.random() < 0.5 for _ in range(100)]
        observed_win_rate = sum(wins) / 100.0

        cal = IsotonicCalibrator().fit(probs, wins)
        # Калибратор должен максимально точно отражать realized hit-rate.
        self.assertAlmostEqual(cal.predict(0.9), observed_win_rate, places=2)

    def test_calibrated_brier_better_or_equal_on_training(self) -> None:
        """IS Brier калибровки ≤ raw Brier (isotonic минимизирует MSE)."""
        random.seed(123)
        probs = [0.6 + random.random() * 0.3 for _ in range(100)]
        wins = [random.random() < (p - 0.1) for p in probs]

        cal = IsotonicCalibrator().fit(probs, wins)
        raw = brier_score(probs, wins)
        cal_p = cal.predict_many(probs)
        cal_b = brier_score(cal_p, wins)

        self.assertLessEqual(cal_b, raw + 1e-9)

    def test_monotonicity_preserved_across_unit_interval(self) -> None:
        """predict() на сетке [0,1] должен быть монотонно неубывающим."""
        random.seed(7)
        probs = sorted([random.random() for _ in range(200)])
        wins = [random.random() < p for p in probs]

        cal = IsotonicCalibrator().fit(probs, wins)
        xs = [i / 100.0 for i in range(101)]
        ys = cal.predict_many(xs)
        for i in range(len(ys) - 1):
            self.assertLessEqual(ys[i], ys[i + 1])

    def test_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            IsotonicCalibrator().fit([], [])

    def test_length_mismatch_raises(self) -> None:
        with self.assertRaises(ValueError):
            IsotonicCalibrator().fit([0.5, 0.6], [True])

    def test_single_point(self) -> None:
        """Один win → predict возвращает 1.0 для любого x."""
        cal = IsotonicCalibrator().fit([0.7], [True])
        self.assertEqual(cal.predict(0.0), 1.0)
        self.assertEqual(cal.predict(0.5), 1.0)
        self.assertEqual(cal.predict(1.0), 1.0)


class IsotonicCalibratorPredictTestCase(unittest.TestCase):
    def test_empty_calibrator_returns_clipped_raw(self) -> None:
        """Не обученный калибратор → возвращает clipped raw p."""
        cal = IsotonicCalibrator()
        self.assertEqual(cal.predict(0.7), 0.7)
        self.assertEqual(cal.predict(1.5), 1.0)
        self.assertEqual(cal.predict(-0.2), 0.0)

    def test_clamps_input(self) -> None:
        probs = [0.2, 0.5, 0.8]
        wins = [False, True, True]
        cal = IsotonicCalibrator().fit(probs, wins)
        # p > 1 → возвращает last level
        # p < 0 → возвращает first level
        self.assertEqual(cal.predict(2.0), cal.predict(1.0))
        self.assertEqual(cal.predict(-1.0), cal.predict(0.0))

    def test_predict_many(self) -> None:
        probs = [0.3, 0.5, 0.7]
        wins = [False, True, True]
        cal = IsotonicCalibrator().fit(probs, wins)
        results = cal.predict_many([0.3, 0.7])
        self.assertEqual(len(results), 2)


class IsotonicCalibratorSerializationTestCase(unittest.TestCase):
    def test_to_dict_from_dict_roundtrip(self) -> None:
        probs = [0.2, 0.3, 0.5, 0.7]
        wins = [False, True, False, True]
        cal = IsotonicCalibrator().fit(probs, wins)

        serialized = cal.to_dict()
        restored = IsotonicCalibrator.from_dict(serialized)

        for p in [0.0, 0.2, 0.5, 0.7, 1.0]:
            self.assertEqual(cal.predict(p), restored.predict(p))

    def test_to_json_from_json_roundtrip(self) -> None:
        probs = [0.4, 0.5, 0.6, 0.7]
        wins = [False, False, True, True]
        cal = IsotonicCalibrator().fit(probs, wins)

        raw = cal.to_json()
        restored = IsotonicCalibrator.from_json(raw)

        for p in [0.0, 0.5, 0.7, 1.0]:
            self.assertEqual(cal.predict(p), restored.predict(p))

    def test_corrupted_from_dict_raises(self) -> None:
        with self.assertRaises(ValueError):
            IsotonicCalibrator.from_dict(
                {"breakpoints": [0.5], "levels": [0.2, 0.7], "n_train": 2}
            )


class FitIsotonicAndEvaluateTestCase(unittest.TestCase):
    def test_overconfident_model_improvement(self) -> None:
        """90%-confident model wins 50% → калибровка значимо улучшает Brier."""
        random.seed(456)
        probs = [0.85] * 50 + [0.95] * 50
        wins = [random.random() < 0.5 for _ in range(100)]

        result = fit_isotonic_and_evaluate(probs, wins)

        # Должно быть улучшение (raw_brier - cal_brier > 0).
        self.assertIsNotNone(result["improvement"])
        self.assertGreater(result["improvement"], 0.0)
        self.assertEqual(result["n"], 100)

    def test_empty_input_returns_zero_metrics(self) -> None:
        result = fit_isotonic_and_evaluate([], [])
        self.assertIsNone(result["raw_brier"])
        self.assertIsNone(result["calibrated_brier"])
        self.assertEqual(result["n"], 0)
        self.assertEqual(result["calibrator"].breakpoints, [])

    def test_well_calibrated_model_small_improvement(self) -> None:
        """Уже хорошо откалиброванная модель → improvement мал."""
        random.seed(789)
        probs = sorted([0.4 + random.random() * 0.5 for _ in range(200)])
        wins = [random.random() < p for p in probs]

        result = fit_isotonic_and_evaluate(probs, wins)

        # Brier баллов 0.20 ± 0.05 на хорошей калибровке.
        self.assertLess(result["raw_brier"], 0.30)
        # Improvement скромный, но не отрицательный (IS).
        self.assertGreaterEqual(result["improvement"], -1e-9)


if __name__ == "__main__":
    unittest.main()
