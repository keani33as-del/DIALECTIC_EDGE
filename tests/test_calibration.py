"""Unit-тесты для core/calibration.py.

Покрывают:
  * `link_provenance_outcomes` — direct join по prediction_id + fuzzy join
    по asset+direction+time-window. Прав ли возврат win/loss/pending.
  * `compute_overall_stats` — hit-rate, Brier, count, avg pnl.
    Малые n не должны выдавать «уверенные» цифры (is_reliable=False).
  * breakdown_by_asset/direction/decision_type/regime — стратификация
    с правильными ключами.
  * `compute_reliability_diagram` — бинирование, calibration_gap.
  * `compute_signal_attribution` — separation wins vs losses на синтетике.
  * `detect_concept_drift` — STABLE / DRIFT / INSUFFICIENT_DATA verdict'ы.
  * `_score_to_probability` / `_is_win` / `_normalize_direction` — helpers.
  * Telegram formatters — не падают и содержат ожидаемые токены.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch


class CalibrationTestCase(unittest.IsolatedAsyncioTestCase):
    """Свой временный SQLite + provenance.ensure_table + ручное создание predictions."""

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="cal_test_")
        self._db_path = os.path.join(self._tmpdir, "test_cal.db")

        self._patches = [
            patch("config.DB_PATH", self._db_path),
            patch("core.provenance.DB_PATH", self._db_path),
            patch("core.calibration.DB_PATH", self._db_path),
        ]
        for p in self._patches:
            p.start()

        from core import provenance
        await provenance.ensure_table()

        # Создаём predictions table вручную (она в database.init_db,
        # но мы не хотим тащить весь init_db чтобы изолировать тесты).
        import aiosqlite
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at   TEXT DEFAULT (datetime('now')),
                    asset        TEXT NOT NULL,
                    direction    TEXT NOT NULL,
                    entry_price  REAL,
                    target_price REAL,
                    stop_loss    REAL,
                    timeframe    TEXT,
                    source_news  TEXT,
                    result       TEXT DEFAULT 'pending',
                    result_price REAL,
                    result_at    TEXT,
                    pnl_pct      REAL,
                    prediction_type TEXT,
                    forecast     TEXT,
                    fact         TEXT,
                    report_type  TEXT DEFAULT 'global'
                )
            """)
            await db.commit()

    async def asyncTearDown(self) -> None:
        for p in self._patches:
            p.stop()
        try:
            os.remove(self._db_path)
            os.rmdir(self._tmpdir)
        except OSError:
            pass

    # ─── helpers ─────────────────────────────────────────────────────────────

    async def _insert_prediction(
        self,
        asset: str,
        direction: str,
        result: str,
        pnl_pct: float = 0.0,
    ) -> int:
        import aiosqlite
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                """
                INSERT INTO predictions
                    (asset, direction, entry_price, target_price, stop_loss,
                     timeframe, source_news, result, pnl_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (asset, direction, 100.0, 110.0, 95.0, "1D", "test",
                 result, pnl_pct),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def _freeze(
        self,
        asset: str,
        direction: str,
        score: int,
        prediction_id: int | None = None,
        regime_trend: str | None = None,
        weights: dict | None = None,
    ) -> int:
        from core.provenance import freeze_scorer_decision, link_prediction
        features: dict = {"price": 100.0}
        if regime_trend is not None:
            features["trend"] = regime_trend
        pid = await freeze_scorer_decision(
            asset=asset,
            direction=direction,
            score=score,
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            sigma_1d_pct=2.0,
            features=features,
            weights=weights or {"total": score, "trend_alignment": score - 30},
        )
        if prediction_id is not None:
            await link_prediction(pid, prediction_id)
        return pid

    # ─── link_provenance_outcomes ────────────────────────────────────────────

    async def test_link_via_prediction_id(self) -> None:
        from core.calibration import link_provenance_outcomes

        pred_id = await self._insert_prediction("BTC", "LONG", "win", 4.5)
        await self._freeze("BTC", "LONG", 75, prediction_id=pred_id)

        linked = await link_provenance_outcomes(window_days=30)
        self.assertEqual(len(linked), 1)
        entry = linked[0]
        self.assertEqual(entry["outcome"], "win")
        self.assertEqual(entry["pnl_pct"], 4.5)
        self.assertEqual(entry["matched_prediction_id"], pred_id)

    async def test_link_via_fuzzy_join(self) -> None:
        """Provenance без prediction_id → fuzzy-joins by asset+direction."""
        from core.calibration import link_provenance_outcomes

        await self._insert_prediction("ETH", "SHORT", "loss", -2.1)
        await self._freeze("ETH", "SHORT", 60, prediction_id=None)

        linked = await link_provenance_outcomes(window_days=30)
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0]["outcome"], "loss")
        self.assertEqual(linked[0]["pnl_pct"], -2.1)

    async def test_unresolved_when_no_matching_prediction(self) -> None:
        from core.calibration import link_provenance_outcomes

        await self._freeze("SOL", "LONG", 65)
        linked = await link_provenance_outcomes(window_days=30)
        self.assertEqual(len(linked), 1)
        self.assertIsNone(linked[0]["outcome"])
        self.assertIsNone(linked[0]["pnl_pct"])

    async def test_filter_by_asset(self) -> None:
        from core.calibration import link_provenance_outcomes

        await self._freeze("BTC", "LONG", 70)
        await self._freeze("ETH", "SHORT", 60)
        await self._freeze("BTC", "SHORT", 55)

        only_btc = await link_provenance_outcomes(window_days=30, asset="BTC")
        self.assertEqual(len(only_btc), 2)
        self.assertTrue(all(e["provenance"]["asset"] == "BTC" for e in only_btc))

    # ─── compute_overall_stats ───────────────────────────────────────────────

    async def test_overall_stats_perfect_calibration(self) -> None:
        """Все decisions выиграны → hit-rate=100%, Brier очень низкий."""
        from core.calibration import compute_overall_stats, link_provenance_outcomes

        for _ in range(10):
            pid = await self._insert_prediction("BTC", "LONG", "win", 2.0)
            await self._freeze("BTC", "LONG", 90, prediction_id=pid)

        linked = await link_provenance_outcomes(window_days=30)
        stats = compute_overall_stats(linked)
        self.assertEqual(stats["n_resolved"], 10)
        self.assertEqual(stats["hit_rate"], 1.0)
        # Brier при p=0.9, y=1: (0.9-1)^2 = 0.01
        self.assertAlmostEqual(stats["brier_mean"] or 0.0, 0.01, places=3)
        self.assertTrue(stats["is_reliable"])

    async def test_overall_stats_worst_calibration(self) -> None:
        """Все decisions проиграны → hit-rate=0, Brier высокий."""
        from core.calibration import compute_overall_stats, link_provenance_outcomes

        for _ in range(10):
            pid = await self._insert_prediction("BTC", "LONG", "loss", -2.0)
            await self._freeze("BTC", "LONG", 90, prediction_id=pid)

        linked = await link_provenance_outcomes(window_days=30)
        stats = compute_overall_stats(linked)
        self.assertEqual(stats["hit_rate"], 0.0)
        # Brier при p=0.9, y=0: (0.9-0)^2 = 0.81
        self.assertAlmostEqual(stats["brier_mean"] or 0.0, 0.81, places=3)

    async def test_overall_stats_excludes_pending_and_caution(self) -> None:
        """Pending / caution / expired исключаются из hit-rate."""
        from core.calibration import compute_overall_stats, link_provenance_outcomes

        # 5 wins + 2 caution + 1 expired (последние два не учитываются)
        for _ in range(5):
            pid = await self._insert_prediction("BTC", "LONG", "win", 2.0)
            await self._freeze("BTC", "LONG", 70, prediction_id=pid)
        for _ in range(2):
            pid = await self._insert_prediction("BTC", "LONG", "caution", 0.0)
            await self._freeze("BTC", "LONG", 70, prediction_id=pid)
        pid = await self._insert_prediction("BTC", "LONG", "expired", 0.0)
        await self._freeze("BTC", "LONG", 70, prediction_id=pid)

        linked = await link_provenance_outcomes(window_days=30)
        stats = compute_overall_stats(linked)
        self.assertEqual(stats["n_total"], 8)
        # Только 5 wins засчитываются как resolved (caution/expired исключены)
        self.assertEqual(stats["n_resolved"], 5)
        self.assertEqual(stats["hit_rate"], 1.0)

    async def test_overall_stats_unreliable_with_few_samples(self) -> None:
        """is_reliable=False если < _MIN_OBS_OVERALL (=10)."""
        from core.calibration import compute_overall_stats, link_provenance_outcomes

        for _ in range(3):
            pid = await self._insert_prediction("BTC", "LONG", "win", 1.0)
            await self._freeze("BTC", "LONG", 60, prediction_id=pid)

        linked = await link_provenance_outcomes(window_days=30)
        stats = compute_overall_stats(linked)
        self.assertFalse(stats["is_reliable"])

    # ─── breakdown_by_* ──────────────────────────────────────────────────────

    async def test_breakdown_by_direction(self) -> None:
        from core.calibration import breakdown_by_direction, link_provenance_outcomes

        # 5 winning LONGs, 5 losing SHORTs
        for _ in range(5):
            pid = await self._insert_prediction("BTC", "LONG", "win", 2.0)
            await self._freeze("BTC", "LONG", 70, prediction_id=pid)
        for _ in range(5):
            pid = await self._insert_prediction("BTC", "SHORT", "loss", -2.0)
            await self._freeze("BTC", "SHORT", 70, prediction_id=pid)

        linked = await link_provenance_outcomes(window_days=30)
        bd = breakdown_by_direction(linked)
        self.assertIn("LONG", bd)
        self.assertIn("SHORT", bd)
        self.assertEqual(bd["LONG"]["hit_rate"], 1.0)
        self.assertEqual(bd["SHORT"]["hit_rate"], 0.0)

    async def test_breakdown_by_asset(self) -> None:
        from core.calibration import breakdown_by_asset, link_provenance_outcomes

        for _ in range(5):
            pid = await self._insert_prediction("BTC", "LONG", "win", 2.0)
            await self._freeze("BTC", "LONG", 70, prediction_id=pid)
        for _ in range(5):
            pid = await self._insert_prediction("ETH", "LONG", "loss", -2.0)
            await self._freeze("ETH", "LONG", 70, prediction_id=pid)

        linked = await link_provenance_outcomes(window_days=30)
        bd = breakdown_by_asset(linked)
        self.assertEqual(bd["BTC"]["hit_rate"], 1.0)
        self.assertEqual(bd["ETH"]["hit_rate"], 0.0)

    async def test_breakdown_by_regime(self) -> None:
        from core.calibration import breakdown_by_regime, link_provenance_outcomes

        # UPTREND wins, DOWNTREND losses, SIDEWAYS mix
        for _ in range(5):
            pid = await self._insert_prediction("BTC", "LONG", "win", 2.0)
            await self._freeze("BTC", "LONG", 70, prediction_id=pid,
                               regime_trend="UPTREND")
        for _ in range(5):
            pid = await self._insert_prediction("BTC", "LONG", "loss", -2.0)
            await self._freeze("BTC", "LONG", 70, prediction_id=pid,
                               regime_trend="DOWNTREND")

        linked = await link_provenance_outcomes(window_days=30)
        bd = breakdown_by_regime(linked)
        self.assertIn("UPTREND", bd)
        self.assertIn("DOWNTREND", bd)
        self.assertEqual(bd["UPTREND"]["hit_rate"], 1.0)
        self.assertEqual(bd["DOWNTREND"]["hit_rate"], 0.0)

    # ─── reliability diagram ─────────────────────────────────────────────────

    async def test_reliability_diagram_well_calibrated(self) -> None:
        """Score=70 → p=0.70, реальный hit-rate=0.70 → calibration_gap≈0."""
        from core.calibration import (
            compute_reliability_diagram,
            link_provenance_outcomes,
        )

        # 7 wins, 3 losses, all with score=70
        for _ in range(7):
            pid = await self._insert_prediction("BTC", "LONG", "win", 2.0)
            await self._freeze("BTC", "LONG", 70, prediction_id=pid)
        for _ in range(3):
            pid = await self._insert_prediction("BTC", "LONG", "loss", -2.0)
            await self._freeze("BTC", "LONG", 70, prediction_id=pid)

        linked = await link_provenance_outcomes(window_days=30)
        diagram = compute_reliability_diagram(linked, n_bins=10)
        # Все попали в бин 70-80%
        bin_70 = next((b for b in diagram if b["bin"] == "70-80%"), None)
        self.assertIsNotNone(bin_70)
        self.assertEqual(bin_70["n"], 10)
        self.assertAlmostEqual(bin_70["actual_hit_rate"], 0.7, places=2)
        # Calibration gap ≈ 0 (predicted ≈ actual)
        self.assertLess(abs(bin_70["calibration_gap"]), 0.1)

    async def test_reliability_diagram_overconfident(self) -> None:
        """Score=90, real hit-rate=50% → calibration_gap высокий отрицательный."""
        from core.calibration import (
            compute_reliability_diagram,
            link_provenance_outcomes,
        )

        for _ in range(5):
            pid = await self._insert_prediction("BTC", "LONG", "win", 2.0)
            await self._freeze("BTC", "LONG", 90, prediction_id=pid)
        for _ in range(5):
            pid = await self._insert_prediction("BTC", "LONG", "loss", -2.0)
            await self._freeze("BTC", "LONG", 90, prediction_id=pid)

        linked = await link_provenance_outcomes(window_days=30)
        diagram = compute_reliability_diagram(linked, n_bins=10)
        bin_90 = next((b for b in diagram if b["bin"] == "90-100%"), None)
        self.assertIsNotNone(bin_90)
        # predicted ≈ 0.9, actual = 0.5 → gap ≈ -0.4
        self.assertAlmostEqual(bin_90["calibration_gap"], -0.4, places=2)

    # ─── signal attribution ──────────────────────────────────────────────────

    async def test_signal_attribution_separation(self) -> None:
        """`trend_alignment` высокий на win, низкий на loss → positive separation."""
        from core.calibration import (
            compute_signal_attribution,
            link_provenance_outcomes,
        )

        for _ in range(5):
            pid = await self._insert_prediction("BTC", "LONG", "win", 2.0)
            await self._freeze("BTC", "LONG", 70, prediction_id=pid,
                               weights={"trend_alignment": 30, "useless": 5})
        for _ in range(5):
            pid = await self._insert_prediction("BTC", "LONG", "loss", -2.0)
            await self._freeze("BTC", "LONG", 70, prediction_id=pid,
                               weights={"trend_alignment": 5, "useless": 5})

        linked = await link_provenance_outcomes(window_days=30)
        attr = compute_signal_attribution(linked)
        self.assertIn("trend_alignment", attr)
        # На wins среднее 30, на losses среднее 5 → separation=25
        self.assertAlmostEqual(attr["trend_alignment"]["separation"], 25.0, places=1)
        # На "useless" обе группы 5 → separation=0
        self.assertAlmostEqual(attr["useless"]["separation"], 0.0, places=1)
        # Сортировка: trend_alignment сверху (больший |separation|)
        first_key = next(iter(attr))
        self.assertEqual(first_key, "trend_alignment")

    # ─── drift detection ─────────────────────────────────────────────────────

    async def test_drift_insufficient_data(self) -> None:
        from core.calibration import detect_concept_drift

        result = await detect_concept_drift(recent_days=14, baseline_days=60)
        self.assertEqual(result["verdict"], "INSUFFICIENT_DATA")
        self.assertFalse(result["drift_detected"])

    async def test_drift_stable_when_similar_brier(self) -> None:
        """Recent и baseline дают похожий Brier → STABLE."""
        from core.calibration import detect_concept_drift

        # 10 wins → recent baseline same data → no drift
        for _ in range(15):
            pid = await self._insert_prediction("BTC", "LONG", "win", 2.0)
            await self._freeze("BTC", "LONG", 70, prediction_id=pid)

        result = await detect_concept_drift(recent_days=60, baseline_days=60)
        self.assertEqual(result["verdict"], "STABLE")
        self.assertFalse(result["drift_detected"])
        self.assertIsNotNone(result["recent_brier"])
        self.assertIsNotNone(result["baseline_brier"])

    # ─── telegram formatters (smoke) ─────────────────────────────────────────

    async def test_format_overall_telegram_smoke(self) -> None:
        from core.calibration import format_overall_telegram

        stats = {
            "n_total": 20, "n_resolved": 18, "n_wins": 12, "n_losses": 6,
            "hit_rate": 0.667, "brier_mean": 0.18,
            "avg_pnl_pct": 1.85, "is_reliable": True,
        }
        text = format_overall_telegram(stats, window_days=30)
        self.assertIn("66.7%", text)
        self.assertIn("0.180", text)
        self.assertIn("30 дней", text)
        self.assertIn("+1.85%", text)

    async def test_format_breakdown_telegram_smoke(self) -> None:
        from core.calibration import format_breakdown_telegram

        bd = {
            "LONG": {"n_total": 10, "n_resolved": 10, "n_wins": 7,
                     "n_losses": 3, "hit_rate": 0.7, "brier_mean": 0.18,
                     "avg_pnl_pct": 1.5, "is_reliable": True},
            "SHORT": {"n_total": 8, "n_resolved": 8, "n_wins": 2,
                      "n_losses": 6, "hit_rate": 0.25, "brier_mean": 0.50,
                      "avg_pnl_pct": -2.0, "is_reliable": False},
        }
        text = format_breakdown_telegram(bd, title="По направлению")
        self.assertIn("LONG", text)
        self.assertIn("SHORT", text)
        self.assertIn("70.0%", text)
        self.assertIn("25.0%", text)

    async def test_format_breakdown_telegram_no_data(self) -> None:
        from core.calibration import format_breakdown_telegram
        self.assertIn("нет данных", format_breakdown_telegram({}, title="X"))

    async def test_format_reliability_telegram_smoke(self) -> None:
        from core.calibration import format_reliability_telegram

        diag = [
            {"bin": "60-70%", "bin_low": 0.6, "bin_high": 0.7,
             "n": 10, "avg_predicted": 0.65,
             "actual_hit_rate": 0.6, "calibration_gap": -0.05},
        ]
        text = format_reliability_telegram(diag)
        self.assertIn("60-70%", text)
        self.assertIn("65.0%", text)
        self.assertIn("60.0%", text)

    async def test_format_drift_telegram_smoke_stable(self) -> None:
        from core.calibration import format_drift_telegram

        drift = {
            "recent_n": 12, "baseline_n": 50,
            "recent_brier": 0.19, "baseline_brier": 0.18,
            "delta": 0.01, "standard_error": 0.08, "z_score": 0.12,
            "drift_detected": False, "verdict": "STABLE",
        }
        text = format_drift_telegram(drift)
        self.assertIn("STABLE", text)
        self.assertIn("0.190", text)

    async def test_format_drift_telegram_drift(self) -> None:
        from core.calibration import format_drift_telegram

        drift = {
            "recent_n": 12, "baseline_n": 50,
            "recent_brier": 0.40, "baseline_brier": 0.18,
            "delta": 0.22, "standard_error": 0.08, "z_score": 2.75,
            "drift_detected": True, "verdict": "DRIFT",
        }
        text = format_drift_telegram(drift)
        self.assertIn("DRIFT", text)
        self.assertIn("0.400", text)


class CalibrationHelpersTestCase(unittest.TestCase):
    """Sync-тесты для чистых helpers (без БД)."""

    def test_score_to_probability_clip(self) -> None:
        from core.calibration import _score_to_probability
        self.assertEqual(_score_to_probability(None), 0.5)
        self.assertEqual(_score_to_probability(0), 0.5)   # clip снизу
        self.assertEqual(_score_to_probability(50), 0.5)
        self.assertEqual(_score_to_probability(70), 0.7)
        self.assertEqual(_score_to_probability(100), 1.0)
        # Out of range — clip
        self.assertEqual(_score_to_probability(150), 1.0)
        self.assertEqual(_score_to_probability(-10), 0.5)

    def test_is_win_classification(self) -> None:
        from core.calibration import _is_win
        self.assertTrue(_is_win("win"))
        self.assertFalse(_is_win("loss"))
        self.assertIsNone(_is_win("caution"))
        self.assertIsNone(_is_win("pending"))
        self.assertIsNone(_is_win("expired"))
        self.assertIsNone(_is_win(None))

    def test_brier_score(self) -> None:
        from core.calibration import _brier_score
        # Perfect: p=1.0 предсказал win → 0
        self.assertEqual(_brier_score(1.0, True), 0.0)
        # Perfect: p=0.0 предсказал loss → 0
        self.assertEqual(_brier_score(0.0, False), 0.0)
        # Coin-flip правильно: p=0.5, win → 0.25
        self.assertEqual(_brier_score(0.5, True), 0.25)
        # Worst: p=1.0 предсказал но loss → 1.0
        self.assertEqual(_brier_score(1.0, False), 1.0)

    def test_normalize_direction(self) -> None:
        from core.calibration import _normalize_direction
        self.assertEqual(_normalize_direction("LONG"), "LONG")
        self.assertEqual(_normalize_direction("long"), "LONG")
        self.assertEqual(_normalize_direction("BUY"), "LONG")
        self.assertEqual(_normalize_direction("BULLISH"), "LONG")
        self.assertEqual(_normalize_direction("SHORT"), "SHORT")
        self.assertEqual(_normalize_direction("SELL"), "SHORT")
        self.assertEqual(_normalize_direction("BEARISH"), "SHORT")
        self.assertIsNone(_normalize_direction("NEUTRAL"))
        self.assertIsNone(_normalize_direction("SIDEWAYS"))
        self.assertIsNone(_normalize_direction(""))


if __name__ == "__main__":
    unittest.main()
