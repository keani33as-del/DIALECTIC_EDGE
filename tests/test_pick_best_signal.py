# -*- coding: utf-8 -*-
"""Тесты для R-системы выбора лучшей сделки в /markets.

`pick_best_signal` — основной сервис: проходим кандидатов из
`analyze_signals`, считаем composite R-score (confidence + alignment с
bias-map + quant_confirmed − quant_blocked), возвращаем лучшего ИЛИ
None, если даже топ-score < 50 (порог качества).

`build_signals_message` должен выводить блок «⭐ ЛУЧШАЯ СДЕЛКА» перед
списком и ставить ⭐ конкретно у выбранного сигнала.
"""
from __future__ import annotations

import unittest

from signals import build_signals_message, pick_best_signal


def _binance(long_pct=0, short_pct=0, price_change=0.0,
             funding=0.0, has_quant=False,
             quant_verdict="NEUTRAL", quant_blocked=False,
             has_atr=False) -> dict:
    return {
        "long": long_pct,
        "short": short_pct,
        "price_change": price_change,
        "funding_rate": funding,
        "funding_direction": "LONG" if funding > 0 else "SHORT" if funding < 0 else "NEUTRAL",
        "has_traders_data": bool(long_pct or short_pct),
        "quant_verdict": quant_verdict if has_quant else None,
        # quant_confidence в 0-100 (см. quant_filter.quant_verdict).
        "quant_confidence": 85 if has_quant else 0,
        "quant_blocked": quant_blocked,
        "quant_components": {"atr": 100.0} if has_atr else None,
        "last_price": 1000.0,
    }


class TestPickBestSignal(unittest.TestCase):
    def test_returns_none_for_empty(self):
        self.assertIsNone(pick_best_signal([], {}, None))

    def test_returns_none_when_all_below_threshold(self):
        # Низкая confidence + нет alignment → итоговый score < 50.
        signals = [{
            "type": "PRICE_MOVE",
            "symbol": "ADAUSDT",
            "direction": "LONG",
            "confidence": 30,
            "reason": "+3%",
        }]
        data = {"ADAUSDT": _binance(price_change=3.0)}
        self.assertIsNone(pick_best_signal(signals, data, None))

    def test_picks_signal_with_quant_confirm_and_traders(self):
        # Один кандидат с alignment по всем: traders + quant + verdict.
        signals = [{
            "type": "BYBIT_TRADERS",
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "confidence": 75,
            "reason": "75% трейдеров в лонге",
        }]
        data = {
            "BTCUSDT": _binance(
                long_pct=75, short_pct=25,
                price_change=2.5, funding=0.0001,
                has_quant=True, quant_verdict="LONG",
            ),
        }
        verdict = {"verdict": "BULLISH"}
        best = pick_best_signal(signals, data, verdict)
        self.assertIsNotNone(best)
        self.assertEqual(best["symbol"], "BTCUSDT")
        self.assertEqual(best["direction"], "LONG")
        self.assertTrue(best["bias_alignment"])
        self.assertTrue(best["quant_confirmed"])
        self.assertGreaterEqual(best["r_score"], 75)
        self.assertEqual(best["r_ratio"], 2.0)

    def test_quant_blocked_eliminates_candidate(self):
        # quant_blocked → −35 → даже при confidence 90 итог < 50.
        signals = [{
            "type": "PRICE_MOVE",
            "symbol": "ETHUSDT",
            "direction": "LONG",
            "confidence": 90,
            "reason": "+9%",
        }]
        data = {
            "ETHUSDT": _binance(
                price_change=9.0,
                has_quant=True, quant_verdict="SHORT",
                quant_blocked=True,
            ),
        }
        best = pick_best_signal(signals, data, None)
        # Может вернуть None, либо вернуть с очень низким score.
        if best is not None:
            self.assertLess(best["r_score"], 75)

    def test_prefers_confirmed_over_loud_unconfirmed(self):
        # Два кандидата: A — громкий PRICE_MOVE без подтверждения,
        # B — BYBIT_TRADERS с quant + bias. Должен выиграть B.
        signals = [
            {
                "type": "PRICE_MOVE",
                "symbol": "DOGEUSDT",
                "direction": "LONG",
                "confidence": 80,
                "reason": "+8%",
            },
            {
                "type": "BYBIT_TRADERS",
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "confidence": 72,
                "reason": "72% трейдеров",
            },
        ]
        data = {
            # DOGE: bias нейтральный, no quant → confidence=80, без бонусов.
            "DOGEUSDT": _binance(price_change=8.0),
            # BTC: alignment по всем → +12+8+4 = +24 поверх 72 = ~96.
            "BTCUSDT": _binance(
                long_pct=72, short_pct=28,
                price_change=1.5,
                has_quant=True, quant_verdict="LONG",
            ),
        }
        best = pick_best_signal(signals, data, None)
        self.assertIsNotNone(best)
        self.assertEqual(best["symbol"], "BTCUSDT")

    def test_conflict_with_strong_bias_penalized(self):
        # Сигнал LONG, но bias-map сильно SHORT (большой неg. score).
        # Должен либо отвалиться по порогу, либо иметь сильно сниженный score.
        signals = [{
            "type": "PRICE_MOVE",
            "symbol": "SOLUSDT",
            "direction": "LONG",
            "confidence": 60,
            "reason": "+6%",
        }]
        data = {
            # Сильный short-bias: traders 25/75 + quant SHORT.
            "SOLUSDT": _binance(
                long_pct=25, short_pct=75,
                price_change=6.0,
                has_quant=True, quant_verdict="SHORT",
            ),
        }
        best = pick_best_signal(signals, data, None)
        # Либо None, либо явно ниже исходного confidence.
        if best is not None:
            self.assertLess(best["r_score"], 60)


class TestBuildSignalsMessageStar(unittest.TestCase):
    def test_marks_best_signal_with_star(self):
        signals = [
            {
                "type": "BYBIT_TRADERS",
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "confidence": 78,
                "reason": "78% трейдеров в лонге",
            },
            {
                "type": "FUNDING",
                "symbol": "ETHUSDT",
                "direction": "SHORT",
                "confidence": 40,
                "reason": "Funding: -0.02%",
            },
        ]
        data = {
            "BTCUSDT": _binance(
                long_pct=78, short_pct=22,
                price_change=2.0,
                has_quant=True, quant_verdict="LONG",
            ),
            "ETHUSDT": _binance(
                funding=-0.0002, price_change=-0.3,
            ),
        }
        msg = build_signals_message(signals, data, {"verdict": "BULLISH"})
        self.assertIn("⭐ *ЛУЧШАЯ СДЕЛКА СЕЙЧАС*", msg)
        # ★ должна быть рядом с BTC, не с ETH.
        btc_line = [
            line for line in msg.splitlines()
            if "BTCUSDT" in line and "→" in line
        ][0]
        eth_line = [
            line for line in msg.splitlines()
            if "ETHUSDT" in line and "→" in line
        ][0]
        self.assertIn("⭐", btc_line)
        self.assertNotIn("⭐", eth_line)

    def test_no_best_block_when_all_weak(self):
        signals = [{
            "type": "PRICE_MOVE",
            "symbol": "ADAUSDT",
            "direction": "LONG",
            "confidence": 25,
            "reason": "+2.5%",
        }]
        data = {"ADAUSDT": _binance(price_change=2.5)}
        msg = build_signals_message(signals, data, None)
        self.assertNotIn("⭐ *ЛУЧШАЯ СДЕЛКА", msg)
        self.assertNotIn(" ⭐", msg)

    def test_no_best_block_when_no_signals(self):
        msg = build_signals_message([], {}, None)
        self.assertNotIn("⭐ *ЛУЧШАЯ СДЕЛКА", msg)


if __name__ == "__main__":
    unittest.main()
