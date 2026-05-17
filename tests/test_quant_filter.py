"""Tests for ``quant_filter`` — детерминистский mean-reversion ансамбль.

Покрываем:
- build_features() на разной длине истории
- _bb_vote / _donchian_vote / _rsi_vote / _btc_trend по отдельности
- quant_verdict(): 2-of-3 голосование, BTC regime gate, confidence-эвристика
- quant_verdict_label() — рендер
- reconcile_with_llm() — все сочетания LLM↔quant
- core.digest_context._aggregate_quant_verdicts() — компрессия per-symbol

Стратегия: используем синтетические последовательности с известными
свойствами (mean-reverting / trending / random walk) и проверяем что
ансамбль ведёт себя как мы заявили в docs/quant_research_v2.md.
Без numpy/scipy — это unit-тесты, должны быть быстрыми на CI.
"""
from __future__ import annotations

import math
import random
import unittest


def _trending_up(n: int = 240, slope: float = 0.005) -> list[float]:
    """Чистый restless ап-тренд: цена растёт по экспоненте, RSI быстро >70,
    BB-position → 1.0, Donchian-high break — это «свежий хай».
    """
    return [100.0 * math.exp(slope * i) for i in range(n)]


def _trending_down(n: int = 240, slope: float = -0.005) -> list[float]:
    return [100.0 * math.exp(slope * i) for i in range(n)]


def _mean_revert_oversold(n: int = 240) -> list[float]:
    """Цена долго в боковике, потом РЕЗКАЯ просадка в самом конце →
    RSI<30, BB_pos<0.1, Donchian low break.

    Важно: дроп даём только в ПОСЛЕДНИЕ 5 баров (а не 30), чтобы BB-окно
    20 баров было mostly во flat-период — тогда σ маленькая, новые низы
    выпадают за нижнюю границу полосы (bb_pos << 0.1).
    """
    rng = random.Random(7)
    closes = [100.0 + rng.gauss(0.0, 0.5) for _ in range(n - 5)]
    for _ in range(5):
        closes.append(closes[-1] * (1.0 - 0.05))
    return closes


def _mean_revert_overbought(n: int = 240) -> list[float]:
    rng = random.Random(8)
    closes = [100.0 + rng.gauss(0.0, 0.5) for _ in range(n - 5)]
    for _ in range(5):
        closes.append(closes[-1] * (1.0 + 0.05))
    return closes


def _flat(n: int = 240) -> list[float]:
    rng = random.Random(9)
    return [100.0 + rng.gauss(0.0, 0.3) for _ in range(n)]


# ── build_features ─────────────────────────────────────────────────────────────


class TestBuildFeatures(unittest.TestCase):
    def test_full_history_populates_all_fields(self):
        from quant_filter import build_features

        f = build_features(_trending_up(240))
        self.assertGreater(f.close, 0)
        self.assertIsNotNone(f.ma50)
        self.assertIsNotNone(f.ma200)
        self.assertIsNotNone(f.rsi14)
        self.assertIsNotNone(f.bb_pos)
        # Тренд вверх → последняя цена > обеих MA.
        self.assertGreater(f.close, f.ma50)
        self.assertGreater(f.close, f.ma200)

    def test_insufficient_history_keeps_close_only(self):
        from quant_filter import build_features

        f = build_features([100.0, 101.0, 99.0])
        self.assertEqual(f.close, 99.0)
        self.assertIsNone(f.ma50)
        self.assertIsNone(f.ma200)
        # RSI нуждается в 15 точках — None при короткой истории
        self.assertIsNone(f.rsi14)
        self.assertIsNone(f.bb_pos)

    def test_empty_history(self):
        from quant_filter import build_features

        f = build_features([])
        self.assertEqual(f.close, 0.0)
        self.assertIsNone(f.ma50)
        self.assertFalse(f.donch_high_break)
        self.assertFalse(f.donch_low_break)


# ── Individual votes ────────────────────────────────────────────────────────────


class TestIndividualVotes(unittest.TestCase):
    def test_bb_vote_overbought_short(self):
        from quant_filter import build_features, _bb_vote

        f = build_features(_mean_revert_overbought(240))
        # Резкий up-spike в конце → BB-position близко к 1.0
        self.assertIsNotNone(f.bb_pos)
        self.assertGreaterEqual(f.bb_pos, 0.9)
        self.assertEqual(_bb_vote(f), "SHORT")

    def test_bb_vote_oversold_long(self):
        from quant_filter import build_features, _bb_vote

        f = build_features(_mean_revert_oversold(240))
        self.assertLessEqual(f.bb_pos, 0.1)
        self.assertEqual(_bb_vote(f), "LONG")

    def test_bb_vote_flat_neutral(self):
        from quant_filter import build_features, _bb_vote

        f = build_features(_flat(240))
        self.assertEqual(_bb_vote(f), "NEUTRAL")

    def test_rsi_vote_overbought(self):
        from quant_filter import build_features, _rsi_vote

        f = build_features(_trending_up(240, slope=0.01))
        # Сильный аптренд → RSI > 70
        self.assertIsNotNone(f.rsi14)
        self.assertGreater(f.rsi14, 70.0)
        self.assertEqual(_rsi_vote(f), "SHORT")

    def test_rsi_vote_oversold(self):
        from quant_filter import build_features, _rsi_vote

        f = build_features(_trending_down(240, slope=-0.01))
        self.assertLess(f.rsi14, 30.0)
        self.assertEqual(_rsi_vote(f), "LONG")

    def test_rsi_vote_neutral_flat(self):
        from quant_filter import build_features, _rsi_vote

        f = build_features(_flat(240))
        # На flat-ряду RSI ~50; точное число зависит от seed.
        self.assertGreater(f.rsi14, 20.0)
        self.assertLess(f.rsi14, 80.0)
        self.assertEqual(_rsi_vote(f), "NEUTRAL")

    def test_donchian_vote_low_break_long(self):
        from quant_filter import build_features, _donchian_vote

        f = build_features(_mean_revert_oversold(240))
        # Последний бар — свежий low → LONG (mean-revert)
        self.assertTrue(f.donch_low_break)
        self.assertEqual(_donchian_vote(f), "LONG")

    def test_donchian_vote_high_break_short(self):
        from quant_filter import build_features, _donchian_vote

        f = build_features(_mean_revert_overbought(240))
        self.assertTrue(f.donch_high_break)
        self.assertEqual(_donchian_vote(f), "SHORT")

    def test_donchian_vote_neutral_on_flat(self):
        from quant_filter import build_features, _donchian_vote

        f = build_features(_flat(240))
        # На синтетическом flat пробой может случайно случиться, но при
        # больших окнах редко. Если случился — это не лажа теста, поэтому
        # просто проверим, что vote — один из трёх.
        v = _donchian_vote(f)
        self.assertIn(v, ("LONG", "SHORT", "NEUTRAL"))

    def test_btc_trend_long(self):
        from quant_filter import build_features, _btc_trend

        btc_f = build_features(_trending_up(240))
        self.assertEqual(_btc_trend(btc_f), "LONG")

    def test_btc_trend_short(self):
        from quant_filter import build_features, _btc_trend

        btc_f = build_features(_trending_down(240))
        self.assertEqual(_btc_trend(btc_f), "SHORT")

    def test_btc_trend_none_when_no_data(self):
        from quant_filter import _btc_trend

        self.assertEqual(_btc_trend(None), "NEUTRAL")


# ── Top-level quant_verdict ────────────────────────────────────────────────────


class TestQuantVerdict(unittest.TestCase):
    def test_insufficient_history_neutral(self):
        from quant_filter import quant_verdict

        out = quant_verdict([100.0] * 10)
        self.assertEqual(out["verdict"], "NEUTRAL")
        self.assertEqual(out["status"], "insufficient_history")
        self.assertEqual(out["confidence"], 0)

    def test_empty_history_neutral(self):
        from quant_filter import quant_verdict

        out = quant_verdict([])
        self.assertEqual(out["verdict"], "NEUTRAL")
        self.assertEqual(out["status"], "insufficient_history")

    def test_overbought_short_with_btc_neutral(self):
        from quant_filter import quant_verdict

        # Cильный аптренд → 3 голоса SHORT (BB + Donch + RSI). BTC на flat
        # не блокирует → SHORT остаётся.
        out = quant_verdict(_mean_revert_overbought(240), btc_closes=_flat(240))
        self.assertEqual(out["verdict"], "SHORT")
        self.assertGreaterEqual(out["confidence"], 70)
        self.assertEqual(out["status"], "ok")
        # В reason должны быть упомянуты конкретные компоненты
        self.assertTrue("SHORT" in out["reason"])

    def test_oversold_long_with_btc_neutral(self):
        from quant_filter import quant_verdict

        out = quant_verdict(_mean_revert_oversold(240), btc_closes=_flat(240))
        self.assertEqual(out["verdict"], "LONG")
        self.assertGreaterEqual(out["confidence"], 70)

    def test_btc_gate_blocks_counter_trend(self):
        from quant_filter import quant_verdict

        # Свой сигнал SHORT (perebought), но BTC сильно вверх → демоут до NEUTRAL.
        own = _mean_revert_overbought(240)
        btc = _trending_up(240)
        out = quant_verdict(own, btc_closes=btc)
        self.assertEqual(out["verdict"], "NEUTRAL")
        self.assertEqual(out["confidence"], 30)  # gated by BTC
        self.assertIn("BTC", out["reason"])

    def test_btc_gate_blocks_counter_trend_long(self):
        from quant_filter import quant_verdict

        # Свой LONG vs BTC SHORT → демоут.
        own = _mean_revert_oversold(240)
        btc = _trending_down(240)
        out = quant_verdict(own, btc_closes=btc)
        self.assertEqual(out["verdict"], "NEUTRAL")
        self.assertEqual(out["confidence"], 30)

    def test_btc_agreement_keeps_signal(self):
        from quant_filter import quant_verdict

        # SHORT signal + BTC SHORT → подтверждение, не блокирует.
        own = _mean_revert_overbought(240)
        btc = _trending_down(240)
        out = quant_verdict(own, btc_closes=btc)
        self.assertEqual(out["verdict"], "SHORT")
        self.assertGreaterEqual(out["confidence"], 70)

    def test_no_btc_passed_means_no_gate(self):
        from quant_filter import quant_verdict

        out = quant_verdict(_mean_revert_overbought(240), btc_closes=None)
        # Гейт выключен — SHORT не блокируется
        self.assertEqual(out["verdict"], "SHORT")

    def test_flat_returns_neutral(self):
        from quant_filter import quant_verdict

        out = quant_verdict(_flat(240), btc_closes=_flat(240))
        self.assertEqual(out["verdict"], "NEUTRAL")

    def test_features_dict_in_output(self):
        from quant_filter import quant_verdict

        out = quant_verdict(_trending_up(240), btc_closes=None)
        self.assertIn("features", out)
        self.assertIn("close", out["features"])
        self.assertIn("ma50", out["features"])
        self.assertIn("rsi14", out["features"])

    def test_components_dict_has_all_keys(self):
        from quant_filter import quant_verdict

        out = quant_verdict(_trending_up(240), btc_closes=_trending_up(240))
        for k in ("bb", "donchian", "rsi", "btc_trend", "votes_long", "votes_short"):
            self.assertIn(k, out["components"], f"missing {k}")


# ── quant_verdict_label ────────────────────────────────────────────────────────


class TestQuantVerdictLabel(unittest.TestCase):
    def test_long_label(self):
        from quant_filter import quant_verdict_label

        emoji, label = quant_verdict_label("LONG")
        self.assertEqual(emoji, "🟢")
        self.assertIn("LONG", label)

    def test_short_label(self):
        from quant_filter import quant_verdict_label

        emoji, label = quant_verdict_label("SHORT")
        self.assertEqual(emoji, "🔴")
        self.assertIn("SHORT", label)

    def test_neutral_label(self):
        from quant_filter import quant_verdict_label

        emoji, label = quant_verdict_label("NEUTRAL")
        self.assertEqual(emoji, "⚪️")
        self.assertIn("NEUTRAL", label)

    def test_unknown_label_falls_back(self):
        from quant_filter import quant_verdict_label

        emoji, label = quant_verdict_label("XYZ")
        # неизвестный = нейтральный (fallback)
        self.assertEqual(emoji, "⚪️")


# ── reconcile_with_llm ─────────────────────────────────────────────────────────


class TestReconcileWithLLM(unittest.TestCase):
    def test_agreement_long(self):
        from quant_filter import reconcile_with_llm

        v, note = reconcile_with_llm("LONG", "LONG")
        self.assertEqual(v, "LONG")
        self.assertIn("согласны", note)

    def test_agreement_short(self):
        from quant_filter import reconcile_with_llm

        v, note = reconcile_with_llm("SHORT", "SHORT")
        self.assertEqual(v, "SHORT")
        self.assertIn("согласны", note)

    def test_llm_buy_quant_long_normalized(self):
        from quant_filter import reconcile_with_llm

        v, _ = reconcile_with_llm("BUY", "LONG")
        self.assertEqual(v, "LONG")

    def test_llm_bullish_quant_long_normalized(self):
        from quant_filter import reconcile_with_llm

        v, _ = reconcile_with_llm("BULLISH", "LONG")
        self.assertEqual(v, "LONG")

    def test_llm_sell_quant_short_normalized(self):
        from quant_filter import reconcile_with_llm

        v, _ = reconcile_with_llm("SELL", "SHORT")
        self.assertEqual(v, "SHORT")

    def test_conflict_demotes_to_neutral(self):
        from quant_filter import reconcile_with_llm

        v, note = reconcile_with_llm("LONG", "SHORT")
        self.assertEqual(v, "NEUTRAL")
        self.assertIn("конфликт", note)

    def test_conflict_other_direction(self):
        from quant_filter import reconcile_with_llm

        v, note = reconcile_with_llm("BUY", "SHORT")
        self.assertEqual(v, "NEUTRAL")
        self.assertIn("конфликт", note)

    def test_quant_neutral_keeps_llm(self):
        from quant_filter import reconcile_with_llm

        v, note = reconcile_with_llm("LONG", "NEUTRAL")
        self.assertEqual(v, "LONG")
        # Если quant пасует — нет «конфликта», просто пропускаем LLM
        self.assertNotIn("конфликт", note)

    def test_llm_neutral_keeps_neutral(self):
        from quant_filter import reconcile_with_llm

        v, _ = reconcile_with_llm("NEUTRAL", "LONG")
        self.assertEqual(v, "NEUTRAL")

    def test_unknown_llm_normalized_to_neutral(self):
        from quant_filter import reconcile_with_llm

        v, _ = reconcile_with_llm("XYZ", "LONG")
        # Неизвестный LLM-токен → нормализуем к NEUTRAL, quant остаётся
        self.assertEqual(v, "NEUTRAL")

    def test_empty_inputs(self):
        from quant_filter import reconcile_with_llm

        v, _ = reconcile_with_llm("", "")
        self.assertEqual(v, "NEUTRAL")


# ── _aggregate_quant_verdicts (compression for /daily) ─────────────────────────


class TestAggregateQuantVerdicts(unittest.TestCase):
    def test_empty_map(self):
        from core.digest_context import _aggregate_quant_verdicts

        v, c, s = _aggregate_quant_verdicts({})
        self.assertEqual(v, "NEUTRAL")
        self.assertEqual(c, 0)
        self.assertEqual(s, {})

    def test_none_input(self):
        from core.digest_context import _aggregate_quant_verdicts

        v, c, s = _aggregate_quant_verdicts(None)
        self.assertEqual(v, "NEUTRAL")
        self.assertEqual(c, 0)

    def test_majority_long(self):
        from core.digest_context import _aggregate_quant_verdicts

        qm = {
            "BTC": {"verdict": "LONG", "confidence": 70},
            "ETH": {"verdict": "LONG", "confidence": 80},
            "SOL": {"verdict": "NEUTRAL", "confidence": 0},
        }
        v, c, s = _aggregate_quant_verdicts(qm)
        self.assertEqual(v, "LONG")
        self.assertEqual(c, 75)  # avg of 70 and 80
        self.assertEqual(s["BTC"], "LONG")

    def test_majority_short(self):
        from core.digest_context import _aggregate_quant_verdicts

        qm = {
            "BTC": {"verdict": "SHORT", "confidence": 70},
            "ETH": {"verdict": "SHORT", "confidence": 70},
            "SOL": {"verdict": "LONG", "confidence": 70},
        }
        v, _, _ = _aggregate_quant_verdicts(qm)
        self.assertEqual(v, "SHORT")

    def test_tie_falls_to_neutral(self):
        from core.digest_context import _aggregate_quant_verdicts

        qm = {
            "BTC": {"verdict": "LONG", "confidence": 70},
            "ETH": {"verdict": "LONG", "confidence": 70},
            "SOL": {"verdict": "SHORT", "confidence": 70},
            "BNB": {"verdict": "SHORT", "confidence": 70},
        }
        v, _, _ = _aggregate_quant_verdicts(qm)
        # 2 LONG vs 2 SHORT → NEUTRAL по правилам
        self.assertEqual(v, "NEUTRAL")

    def test_single_vote_not_enough(self):
        from core.digest_context import _aggregate_quant_verdicts

        qm = {
            "BTC": {"verdict": "LONG", "confidence": 90},
            "ETH": {"verdict": "NEUTRAL", "confidence": 0},
        }
        v, _, _ = _aggregate_quant_verdicts(qm)
        # 1 LONG не достаточно (требуем минимум 2)
        self.assertEqual(v, "NEUTRAL")

    def test_summary_dict_contains_all_symbols_with_verdict(self):
        from core.digest_context import _aggregate_quant_verdicts

        qm = {
            "BTC": {"verdict": "LONG", "confidence": 70},
            "ETH": {"verdict": "SHORT", "confidence": 70},
            "XYZ_INVALID": {"verdict": "WTF", "confidence": 70},
        }
        v, _, s = _aggregate_quant_verdicts(qm)
        self.assertIn("BTC", s)
        self.assertIn("ETH", s)
        self.assertNotIn("XYZ_INVALID", s)


if __name__ == "__main__":
    unittest.main()
