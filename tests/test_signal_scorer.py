"""Тесты для `core.signal_scorer`.

Зеркалят контракт из docstring модуля:
  • Score 0-100 разбит на 5 компонентов (30+20+20+15+15).
  • Direction = LONG / SHORT / NONE.
  • Setup строится только при score ≥ min_score, direction != NONE,
    asset в TRADABLE_ASSETS, и есть σ̂.
  • SL/TP считаются от entry × (1 ± k·σ̂); R/R = 2.0x до округления.
  • Округление до tick (XRP=0.1, BTC=0.01 — критично, иначе Bybit
    отвергнет ордер).
"""

from __future__ import annotations

import unittest

from core.signal_scorer import (
    ASSET_TICK_SIZE,
    DEFAULT_MIN_SCORE,
    SL_SIGMA_MULT,
    TP_SIGMA_MULT,
    TRADABLE_ASSETS,
    AssetScore,
    ScoreBreakdown,
    SignalSetup,
    _round_to_tick,
    make_setup,
    rank_signals,
    score_asset,
)


# ────────────────────────── фикстуры ──────────────────────────────────────


def _strong_uptrend_sol() -> dict:
    """SOL в чистом uptrend — все компоненты «за» LONG.

    Это «золотой» актив для нашей системы: max score должен быть
    ~95-100/100. Используется для верификации что максимум вообще
    достижим (sanity check).
    """
    return {
        "price": 90.58,
        "trend": "UPTREND",
        "ma50": 86.0,
        "ma200": 80.0,
        "complexity_hint": "TRENDING",
        "hurst": 0.61,
        "tradeable_score": 0.72,
        "vrt_random_walk": False,
        "vrt_ratio": 1.18,
        "markov_state": "UP",
        "markov_next_probs": {"UP": 0.55, "FLAT": 0.25, "DOWN": 0.20},
        "vol_sigma_1d_pct": 3.20,
    }


def _xrp_sideways_yesterday() -> dict:
    """XRP вчера — пограничный SIDEWAYS, score 0.52 (ниже порога 0.6).

    Кейс из live: user открыл XRP, поехал в минус, SL зафиксировал
    -$0.13. Команда `/signal` должна была сказать «не торгуй».
    Эта фикстура — регрессия.
    """
    return {
        "price": 1.4643,
        "trend": "SIDEWAYS",
        "ma50": 1.30,
        "ma200": 1.65,
        "complexity_hint": "MEAN_REVERTING",
        "hurst": 0.42,
        "tradeable_score": 0.52,
        "vrt_random_walk": True,
        "markov_state": "FLAT",
        "markov_next_probs": {"UP": 0.33, "FLAT": 0.40, "DOWN": 0.27},
        "vol_sigma_1d_pct": 2.00,
    }


def _strong_downtrend_btc() -> dict:
    """BTC в чистом downtrend — все компоненты «за» SHORT."""
    return {
        "price": 65000.0,
        "trend": "DOWNTREND",
        "ma50": 68000.0,
        "ma200": 72000.0,
        "complexity_hint": "TRENDING",
        "hurst": 0.62,
        "tradeable_score": 0.70,
        "vrt_random_walk": False,
        "markov_state": "DOWN",
        "markov_next_probs": {"UP": 0.20, "FLAT": 0.25, "DOWN": 0.55},
        "vol_sigma_1d_pct": 2.50,
    }


# ────────────────────────── ScoreBreakdown ────────────────────────────────


class TestScoreBreakdown(unittest.TestCase):
    def test_max_total_is_100(self):
        # 30 + 20 + 20 + 15 + 15 = 100 — суммируется ровно к 100.
        bd = ScoreBreakdown(
            trend_alignment=30,
            complexity_hint=20,
            vrt_structure=20,
            markov_state=15,
            raw_tradeable=15,
        )
        self.assertEqual(bd.total, 100)

    def test_total_clamps_to_100(self):
        # Защита от переполнения, если кто-то выдаст >max в компоненте.
        bd = ScoreBreakdown(
            trend_alignment=50,
            complexity_hint=20,
            vrt_structure=20,
            markov_state=15,
            raw_tradeable=15,
        )
        self.assertEqual(bd.total, 100)

    def test_zero_default(self):
        self.assertEqual(ScoreBreakdown().total, 0)


# ────────────────────────── score_asset ───────────────────────────────────


class TestScoreAsset(unittest.TestCase):
    def test_strong_uptrend_scores_high(self):
        s = score_asset("SOL", _strong_uptrend_sol())
        self.assertEqual(s.direction, "LONG")
        self.assertGreaterEqual(s.total, 90)
        # Компоненты по 30+20+20+15 — без raw_tradeable должно быть 85,
        # с raw_tradeable=11 (0.72×15) = 96.
        self.assertEqual(s.breakdown.trend_alignment, 30)
        self.assertEqual(s.breakdown.complexity_hint, 20)
        self.assertEqual(s.breakdown.vrt_structure, 20)
        self.assertEqual(s.breakdown.markov_state, 15)
        self.assertEqual(s.breakdown.raw_tradeable, 11)

    def test_strong_downtrend_short(self):
        s = score_asset("BTC", _strong_downtrend_btc())
        self.assertEqual(s.direction, "SHORT")
        self.assertGreaterEqual(s.total, 90)
        self.assertEqual(s.breakdown.markov_state, 15)  # P(DOWN)=55% ≥ 40%

    def test_xrp_sideways_no_trade(self):
        # Регрессия: вчерашний кейс — XRP NEUTRAL, score < 60, без сетапа.
        s = score_asset("XRP", _xrp_sideways_yesterday())
        self.assertEqual(s.direction, "NONE")
        self.assertEqual(s.total, 0)
        # Только trend-компонент посчитан (0 pts), остальные не запускались.
        self.assertEqual(s.breakdown.trend_alignment, 0)
        self.assertEqual(s.breakdown.complexity_hint, 0)
        self.assertEqual(s.breakdown.vrt_structure, 0)
        self.assertEqual(s.breakdown.markov_state, 0)
        self.assertEqual(s.breakdown.raw_tradeable, 0)

    def test_missing_complexity_hint_gives_neutral_5(self):
        p = _strong_uptrend_sol()
        del p["complexity_hint"]
        s = score_asset("SOL", p)
        self.assertEqual(s.breakdown.complexity_hint, 5)

    def test_mean_reverting_in_uptrend_gives_5(self):
        # complexity_hint = MEAN_REVERTING — это контр-trend, weak edge.
        p = _strong_uptrend_sol()
        p["complexity_hint"] = "MEAN_REVERTING"
        s = score_asset("SOL", p)
        self.assertEqual(s.breakdown.complexity_hint, 5)

    def test_random_walk_complexity_zero(self):
        p = _strong_uptrend_sol()
        p["complexity_hint"] = "RANDOM_WALK"
        s = score_asset("SOL", p)
        self.assertEqual(s.breakdown.complexity_hint, 0)

    def test_vrt_rejects_h0_full_20(self):
        s = score_asset("SOL", _strong_uptrend_sol())
        self.assertEqual(s.breakdown.vrt_structure, 20)

    def test_vrt_h0_not_rejected_zero(self):
        p = _strong_uptrend_sol()
        p["vrt_random_walk"] = True
        s = score_asset("SOL", p)
        self.assertEqual(s.breakdown.vrt_structure, 0)

    def test_vrt_missing_gives_5(self):
        p = _strong_uptrend_sol()
        del p["vrt_random_walk"]
        s = score_asset("SOL", p)
        self.assertEqual(s.breakdown.vrt_structure, 5)

    def test_markov_against_trade_zero(self):
        # LONG-кандидат + Markov DOWN → 0 pts (против).
        p = _strong_uptrend_sol()
        p["markov_state"] = "DOWN"
        p["markov_next_probs"] = {"UP": 0.2, "FLAT": 0.2, "DOWN": 0.6}
        s = score_asset("SOL", p)
        self.assertEqual(s.breakdown.markov_state, 0)

    def test_markov_flat_gives_5(self):
        p = _strong_uptrend_sol()
        p["markov_state"] = "FLAT"
        p["markov_next_probs"] = {"UP": 0.33, "FLAT": 0.34, "DOWN": 0.33}
        s = score_asset("SOL", p)
        self.assertEqual(s.breakdown.markov_state, 5)

    def test_markov_same_state_low_prob_10pts(self):
        # Markov UP, но P(next UP) < 40% → 10, не 15.
        p = _strong_uptrend_sol()
        p["markov_next_probs"] = {"UP": 0.35, "FLAT": 0.35, "DOWN": 0.30}
        s = score_asset("SOL", p)
        self.assertEqual(s.breakdown.markov_state, 10)

    def test_raw_score_zero_when_missing(self):
        p = _strong_uptrend_sol()
        del p["tradeable_score"]
        s = score_asset("SOL", p)
        self.assertEqual(s.breakdown.raw_tradeable, 0)

    def test_raw_score_clamps_to_1(self):
        # Защита: если кто-то передал score=1.5 — clamp до 15.
        p = _strong_uptrend_sol()
        p["tradeable_score"] = 1.5
        s = score_asset("SOL", p)
        self.assertEqual(s.breakdown.raw_tradeable, 15)

    def test_reasons_are_human_readable(self):
        s = score_asset("SOL", _strong_uptrend_sol())
        # Должны быть UTF-8 строки с символом «✓», без plain Python repr.
        text = " ".join(s.reasons)
        self.assertIn("UPTREND", text)
        self.assertIn("TRENDING", text)
        self.assertIn("VRT", text)
        self.assertIn("Markov", text)


# ────────────────────────── make_setup ────────────────────────────────────


class TestMakeSetup(unittest.TestCase):
    def test_strong_long_produces_setup(self):
        s = score_asset("SOL", _strong_uptrend_sol())
        setup = make_setup(s, _strong_uptrend_sol(), capital=123.0)
        self.assertIsNotNone(setup)
        self.assertEqual(setup.asset, "SOL")
        self.assertEqual(setup.direction, "LONG")
        # Entry около 90.58.
        self.assertAlmostEqual(setup.entry, 90.58, places=2)
        # SL = entry × (1 - 1.5 × 0.032) = entry × 0.952 ≈ 86.23
        self.assertLess(setup.stop, setup.entry)
        self.assertAlmostEqual(setup.stop, 90.58 * (1 - 1.5 * 0.032), places=1)
        # TP = entry × (1 + 3.0 × 0.032) = entry × 1.096 ≈ 99.28
        self.assertGreater(setup.target, setup.entry)
        self.assertAlmostEqual(setup.target, 90.58 * (1 + 3.0 * 0.032), places=1)
        # R/R должен быть ≈ 2.0
        self.assertAlmostEqual(setup.rr_ratio, 2.0, places=1)
        # Size = 25% × 123 = $30.75
        self.assertAlmostEqual(setup.size_usd, 30.75, places=2)
        # Score сохраняется
        self.assertEqual(setup.score, s.total)

    def test_strong_short_produces_setup(self):
        s = score_asset("BTC", _strong_downtrend_btc())
        setup = make_setup(s, _strong_downtrend_btc(), capital=123.0)
        self.assertIsNotNone(setup)
        self.assertEqual(setup.direction, "SHORT")
        # Для SHORT: stop ВЫШЕ entry, target НИЖЕ entry.
        self.assertGreater(setup.stop, setup.entry)
        self.assertLess(setup.target, setup.entry)
        # stop_pct положительный (стоп выше), target_pct отрицательный.
        self.assertGreater(setup.stop_pct, 0)
        self.assertLess(setup.target_pct, 0)

    def test_sideways_no_setup(self):
        s = score_asset("XRP", _xrp_sideways_yesterday())
        setup = make_setup(s, _xrp_sideways_yesterday())
        self.assertIsNone(setup)

    def test_low_score_no_setup(self):
        # Принудительно низкий score → нет setup.
        p = _strong_uptrend_sol()
        p["vrt_random_walk"] = True
        p["complexity_hint"] = "RANDOM_WALK"
        p["markov_state"] = "FLAT"
        p["markov_next_probs"] = {"UP": 0.33, "FLAT": 0.34, "DOWN": 0.33}
        p["tradeable_score"] = 0.3
        s = score_asset("SOL", p)
        # total = 30 (trend) + 0 + 0 + 5 (flat) + 4 (0.3×15) = 39 < 60
        self.assertLess(s.total, DEFAULT_MIN_SCORE)
        setup = make_setup(s, p)
        self.assertIsNone(setup)

    def test_missing_sigma_no_setup(self):
        # Без σ̂ — не строим setup даже при high score.
        p = _strong_uptrend_sol()
        del p["vol_sigma_1d_pct"]
        s = score_asset("SOL", p)
        self.assertGreaterEqual(s.total, DEFAULT_MIN_SCORE)
        setup = make_setup(s, p)
        self.assertIsNone(setup)

    def test_xrp_tick_rounding(self):
        # XRP на Bybit Spot: tick=0.1 (1 знак после точки).
        # Если entry=1.4643, σ̂=2%, то SL = 1.4643 * (1 - 1.5×0.02) = 1.4204
        # — округляется до 1.4 (tick=0.1).
        p = _strong_uptrend_sol()
        p["price"] = 1.4643
        p["ma50"] = 1.40
        p["ma200"] = 1.20
        p["vol_sigma_1d_pct"] = 2.00
        s = score_asset("XRP", p)
        setup = make_setup(s, p)
        self.assertIsNotNone(setup)
        # tick=0.1 → одна цифра после точки
        for price in (setup.entry, setup.stop, setup.target):
            self.assertAlmostEqual(price * 10, round(price * 10), places=4)

    def test_indices_not_tradable(self):
        # SPX/NDX/VIX — не в TRADABLE_ASSETS, setup не строим.
        p = _strong_uptrend_sol()
        p["price"] = 7500.0
        s = score_asset("SPX", p)
        self.assertNotIn("SPX", TRADABLE_ASSETS)
        setup = make_setup(s, p)
        self.assertIsNone(setup)

    def test_custom_min_score_threshold(self):
        # min_score=80 — даже SOL с 96/100 пройдёт.
        s = score_asset("SOL", _strong_uptrend_sol())
        setup = make_setup(s, _strong_uptrend_sol(), min_score=80)
        self.assertIsNotNone(setup)
        # min_score=99 — не пройдёт.
        setup = make_setup(s, _strong_uptrend_sol(), min_score=99)
        self.assertIsNone(setup)

    def test_size_fraction_scales(self):
        s = score_asset("SOL", _strong_uptrend_sol())
        setup = make_setup(s, _strong_uptrend_sol(), capital=200.0, size_fraction=0.5)
        self.assertEqual(setup.size_usd, 100.0)


# ────────────────────────── rank_signals ──────────────────────────────────


class TestRankSignals(unittest.TestCase):
    def test_returns_top_and_scored_list(self):
        prices = {
            "BTC": _strong_downtrend_btc(),
            "SOL": _strong_uptrend_sol(),
            "XRP": _xrp_sideways_yesterday(),
        }
        result = rank_signals(prices, capital=123.0)
        self.assertIn("top", result)
        self.assertIn("scored", result)
        # Top setup должен быть либо SOL (LONG) либо BTC (SHORT) —
        # оба имеют высокие score, чей выше — победитель.
        self.assertIsNotNone(result["top"])
        self.assertIn(result["top"].asset, ("SOL", "BTC"))
        # Scored — все 3 в порядке убывания.
        scored = result["scored"]
        self.assertEqual(len(scored), 3)
        self.assertGreaterEqual(scored[0].total, scored[1].total)
        self.assertGreaterEqual(scored[1].total, scored[2].total)
        # XRP в самом низу с 0
        self.assertEqual(scored[-1].asset, "XRP")
        self.assertEqual(scored[-1].total, 0)

    def test_skips_helper_keys(self):
        # Ключи MA50_BTC / ATR_BTC должны игнорироваться — это
        # вспомогательные «прокидывания», не активы.
        prices = {
            "SOL": _strong_uptrend_sol(),
            "MA50_BTC": 86000.0,
            "ATR_BTC": 1500.0,
            "MA200_ETH": 4000.0,
        }
        result = rank_signals(prices)
        assets = [s.asset for s in result["scored"]]
        self.assertIn("SOL", assets)
        self.assertNotIn("MA50_BTC", assets)
        self.assertNotIn("ATR_BTC", assets)
        self.assertNotIn("MA200_ETH", assets)

    def test_no_setup_when_all_sideways(self):
        # Все активы в SIDEWAYS → top=None.
        prices = {"XRP": _xrp_sideways_yesterday()}
        result = rank_signals(prices)
        self.assertIsNone(result["top"])

    def test_skips_non_dict_entries(self):
        # market_prices может содержать float-значения (например, ATR_BTC=1500.0).
        # rank_signals не должен падать.
        prices = {
            "SOL": _strong_uptrend_sol(),
            "ATR_BTC": 1500.0,        # float, не dict
            "_meta": "string",        # тоже не dict
        }
        result = rank_signals(prices)
        self.assertEqual(len(result["scored"]), 1)
        self.assertEqual(result["scored"][0].asset, "SOL")

    def test_skips_assets_without_price(self):
        prices = {
            "SOL": _strong_uptrend_sol(),
            "BROKEN": {"trend": "UPTREND"},  # нет price
        }
        result = rank_signals(prices)
        assets = [s.asset for s in result["scored"]]
        self.assertIn("SOL", assets)
        self.assertNotIn("BROKEN", assets)


# ────────────────────────── _round_to_tick ────────────────────────────────


class TestRoundToTick(unittest.TestCase):
    def test_xrp_tick_01(self):
        # 1.4643 → 1.5 (ближайшая до 0.1).
        self.assertEqual(_round_to_tick(1.4643, 0.1), 1.5)
        # 1.4297 → 1.4 (ближайшая до 0.1).
        self.assertEqual(_round_to_tick(1.4297, 0.1), 1.4)

    def test_btc_tick_001(self):
        self.assertAlmostEqual(_round_to_tick(79199.999, 0.01), 79200.00, places=2)
        self.assertAlmostEqual(_round_to_tick(79199.50001, 0.01), 79199.50, places=2)

    def test_sol_tick_0001(self):
        self.assertAlmostEqual(_round_to_tick(90.5814159, 0.001), 90.581, places=3)

    def test_zero_tick_fallback(self):
        # Безопасный fallback на 4 знака.
        self.assertAlmostEqual(_round_to_tick(1.23456789, 0.0), 1.2346, places=4)


# ────────────────────────── constants sanity ──────────────────────────────


class TestConstants(unittest.TestCase):
    def test_rr_ratio_is_2(self):
        # SL_MULT=1.5, TP_MULT=3.0 → R/R = 2.0
        self.assertAlmostEqual(TP_SIGMA_MULT / SL_SIGMA_MULT, 2.0, places=2)

    def test_xrp_tick_is_one_decimal(self):
        # Bybit Spot constraint — XRP price принимается с 1 знаком после точки.
        self.assertEqual(ASSET_TICK_SIZE["XRP"], 0.1)

    def test_tradable_assets_are_crypto_only(self):
        self.assertEqual(TRADABLE_ASSETS, frozenset({"BTC", "ETH", "SOL", "BNB", "XRP"}))


if __name__ == "__main__":
    unittest.main()
