"""Тесты для `_fmt_signal_message` (рендер кнопки «Лучшая сделка»).

Закрывают форматы обеих веток:
  • setup есть → блоки «Почему эта сделка / Вход-Stop-Target / Риски».
  • setup нет → «Сидим» + топ-3 кандидата, БЕЗ старой фразы про
    «~90% дней / слив комиссий».
"""

from __future__ import annotations

import os
import sys
import unittest

# main.py пытается импортить aiogram. Если его нет в окружении (unit-fast
# job) — пропускаем весь модуль.
try:
    import aiogram  # noqa: F401

    HAS_AIOGRAM = True
except Exception:
    HAS_AIOGRAM = False

# Заглушки секретов чтобы main.py смог импортнуться без BOT_TOKEN.
os.environ.setdefault("BOT_TOKEN", "test:test")

from core.signal_scorer import AssetScore, ScoreBreakdown, SignalSetup  # noqa: E402


def _make_setup(score: int = 75) -> SignalSetup:
    """Sample SignalSetup, как его вернул бы rank_signals для SOL LONG."""
    return SignalSetup(
        asset="SOL",
        direction="LONG",
        entry=90.58,
        stop=86.23,
        target=99.28,
        stop_pct=-4.80,
        target_pct=9.60,
        rr_ratio=2.0,
        sigma_1d_pct=3.20,
        size_usd=30.75,
        score=score,
        reasons=[
            "UPTREND ✓ (цена выше MA50 +5.3%, MA200 +13.2%)",
            "TRENDING ✓ (H=0.61, score=0.72)",
            "VRT H0 отвергнут ✓ (VR=1.18, есть структура)",
            "Markov UP ✓ (P(next UP)=55%)",
            "raw score=0.72 → 11 pts",
        ],
    )


def _make_score(asset: str, total: int, direction: str = "LONG") -> AssetScore:
    """Lightweight AssetScore с нужным total — для проверки runner-up логики."""
    bd = ScoreBreakdown()
    # Trend-alignment даёт 30 если direction != NONE, остальное добиваем
    # raw_tradeable'ом (он clamp'ится в [0, 15] внутри ScoreBreakdown.total
    # через clamp(0..100), так что переполнение ОК — отдельно тестируется
    # в test_signal_scorer.py).
    bd.trend_alignment = 30 if direction != "NONE" else 0
    bd.raw_tradeable = max(0, total - bd.trend_alignment)
    reasons = (
        ["UPTREND ✓ (цена выше MA50)"] if direction != "NONE" else ["SIDEWAYS"]
    )
    return AssetScore(asset=asset, direction=direction, breakdown=bd, reasons=reasons)


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestFmtSignalMessageSetupFound(unittest.TestCase):
    """Когда top != None — проверяем что появились новые блоки."""

    @classmethod
    def setUpClass(cls):
        # Импорт main лениво, чтобы не падать в unit-fast (нет aiogram).
        from main import _fmt_signal_message  # noqa: PLC0415

        cls._fmt = staticmethod(_fmt_signal_message)

    def _render(self, **kwargs) -> str:
        top = kwargs.get("top", _make_setup())
        scored = kwargs.get(
            "scored",
            [
                _make_score("SOL", 75),
                _make_score("ETH", 60),
                _make_score("BTC", 45),
            ],
        )
        return self._fmt(
            {
                "top": top,
                "scored": scored,
                "capital": 123.0,
                "min_score": 60,
            }
        )

    def test_header_present(self):
        msg = self._render()
        self.assertIn("АВТО-СИГНАЛ", msg)
        self.assertIn("SOL", msg)
        self.assertIn("LONG", msg)

    def test_why_this_trade_block(self):
        """Должен быть блок «Почему эта сделка» с отрывом от #2."""
        msg = self._render()
        self.assertIn("Почему эта сделка", msg)
        # Отрыв от #2 (ETH 60/100): 75 - 60 = 15 pts.
        self.assertIn("Отрыв от #2", msg)
        self.assertIn("ETH 60/100", msg)
        self.assertIn("+15 pts", msg)

    def test_rr_breakeven_winrate(self):
        """R/R = 2x → breakeven winrate = 100/3 ≈ 33%."""
        msg = self._render()
        self.assertIn("R/R = 2.0x", msg)
        self.assertIn("winrate ≥ 33%", msg)

    def test_entry_stop_target_block(self):
        msg = self._render()
        self.assertIn("Вход / Stop / Target", msg)
        self.assertIn("Entry:", msg)
        self.assertIn("$90.58", msg)
        self.assertIn("Stop:", msg)
        self.assertIn("$86.23", msg)
        self.assertIn("Target:", msg)
        self.assertIn("$99.28", msg)
        self.assertIn("если хит, выходим", msg)
        self.assertIn("фиксируем профит", msg)

    def test_risks_block_with_usd_amounts(self):
        msg = self._render()
        self.assertIn("Риски этой сделки", msg)
        # SL loss = 30.75 * 4.80 / 100 = $1.476 → отображается как $1.48
        self.assertIn("$1.48", msg)
        # TP gain = 30.75 * 9.60 / 100 = $2.952 → $2.95
        self.assertIn("$2.95", msg)
        # % от капитала
        self.assertIn("% от капитала", msg)
        # σ̂ запас от шума
        self.assertIn("σ запаса", msg)

    def test_no_legacy_90_percent_phrase(self):
        """Старая фраза про «~90% дней» не должна появляться даже если top есть."""
        msg = self._render()
        self.assertNotIn("90% дней", msg)
        self.assertNotIn("слив комиссий", msg)

    def test_no_runner_up_when_only_one_scored(self):
        msg = self._render(scored=[_make_score("SOL", 75)])
        self.assertIn("лучший среди 1 сканированных", msg)
        self.assertNotIn("Отрыв от #2", msg)

    def test_weak_marker_surfaced_in_risks(self):
        """Если в reasons есть «0 pts» — выносим в риски как слабое место."""
        top = _make_setup()
        top.reasons = list(top.reasons) + ["VRT не отвергает H0 (ряд похож на random walk)"]
        msg = self._render(top=top)
        self.assertIn("Слабое место", msg)
        self.assertIn("VRT не отвергает H0", msg)

    def test_disclaimer_still_present(self):
        msg = self._render()
        self.assertIn("suggestion, не приказ", msg)
        self.assertIn("Bybit", msg)


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestFmtSignalMessageNoSetup(unittest.TestCase):
    """Когда top == None — старая «лишняя инфа» должна быть удалена."""

    @classmethod
    def setUpClass(cls):
        from main import _fmt_signal_message  # noqa: PLC0415

        cls._fmt = staticmethod(_fmt_signal_message)

    def _render(self, scored=None) -> str:
        if scored is None:
            scored = [
                _make_score("VIX", 71, direction="LONG"),
                _make_score("GOLD", 53, direction="SHORT"),
                _make_score("ETH", 46, direction="SHORT"),
            ]
        return self._fmt(
            {
                "top": None,
                "scored": scored,
                "capital": 123.0,
                "min_score": 60,
            }
        )

    def test_sit_out_header(self):
        msg = self._render()
        self.assertIn("чистого setup нет", msg)
        self.assertIn("Сидим", msg)

    def test_top_3_still_shown(self):
        msg = self._render()
        self.assertIn("VIX", msg)
        self.assertIn("GOLD", msg)
        self.assertIn("ETH", msg)
        self.assertIn("71/100", msg)

    def test_no_90_percent_phrase(self):
        """Главная цель PR-4 — убрать «Это нормально — наша модель велит сидеть»."""
        msg = self._render()
        self.assertNotIn("Это нормально", msg)
        self.assertNotIn("90% дней", msg)
        self.assertNotIn("слив комиссий", msg)

    def test_markets_hint_present(self):
        msg = self._render()
        self.assertIn("/markets", msg)


if __name__ == "__main__":
    unittest.main()
