"""Unit-тесты для core/walk_forward.py.

Покрывают:
  * `split_resolved_by_time` — корректность нарезки на фолды
    (no leakage, правильные временные границы, step_days).
  * `evaluate_fold` — None при insufficient data; metrics для нормального фолда.
  * `aggregate_folds` — verdict логика (CALIBRATION_HELPS / RAW_BETTER / INSUFFICIENT_DATA).
  * `walk_forward_backtest` (async) — end-to-end на временной БД с provenance + predictions.
  * Telegram-rendering helpers (`format_backtest_telegram`, ...).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch


# ─── Sync helpers (split, evaluate, aggregate) ───────────────────────────────


def _make_entry(score: int, outcome: str, hours_ago: float) -> dict:
    """Билдер entry для split/evaluate тестов."""
    ts = datetime(2026, 5, 1, 12, 0, 0) - timedelta(hours=hours_ago)
    return {
        "_ts": ts,
        "outcome": outcome,
        "pnl_pct": 1.0 if outcome == "win" else -1.0,
        "provenance": {
            "asset": "BTC",
            "direction": "LONG",
            "score": score,
            "decision_type": "signal_scorer",
            "weights_json": {"trend_alignment": 30, "total": score},
            "regime_json": {"trend": "UPTREND"},
            "created_at": ts.isoformat(),
        },
    }


class SplitResolvedByTimeTestCase(unittest.TestCase):
    def test_empty_returns_no_folds(self) -> None:
        from core.walk_forward import split_resolved_by_time

        self.assertEqual(
            split_resolved_by_time([], train_days=7, test_days=3, step_days=3), []
        )

    def test_basic_split_two_folds(self) -> None:
        from core.walk_forward import split_resolved_by_time

        # 30 точек, разнесённых по 1 точке на день, 30 дней назад до сейчас.
        entries = [_make_entry(70, "win", hours_ago=i * 24) for i in range(30)]
        for e in entries:
            e["_ts"] = _make_entry(70, "win", hours_ago=0)["_ts"]
        # Чтобы аккуратнее: вручную проставим _ts
        base = datetime(2026, 5, 1, 12)
        for i, e in enumerate(entries):
            e["_ts"] = base - timedelta(days=29 - i)

        folds = split_resolved_by_time(entries, train_days=7, test_days=3, step_days=3)

        # train+test=10 дней, шаг 3 → должно быть ≈ (30-10)/3 + 1 = 7+1 фолдов
        self.assertGreaterEqual(len(folds), 5)
        for f in folds:
            self.assertLess(f["train_end"], f["test_end"])
            self.assertLess(f["train_start"], f["train_end"])

    def test_train_test_no_overlap(self) -> None:
        """Train и test не должны иметь общих точек по времени."""
        from core.walk_forward import split_resolved_by_time

        entries = [_make_entry(70, "win", hours_ago=0) for _ in range(20)]
        base = datetime(2026, 5, 1)
        for i, e in enumerate(entries):
            e["_ts"] = base + timedelta(days=i)

        folds = split_resolved_by_time(entries, train_days=7, test_days=3, step_days=3)
        for f in folds:
            train_ts = {e["_ts"] for e in f["train"]}
            test_ts = {e["_ts"] for e in f["test"]}
            self.assertTrue(train_ts.isdisjoint(test_ts))


class EvaluateFoldTestCase(unittest.TestCase):
    def test_insufficient_train_returns_none(self) -> None:
        from core.walk_forward import evaluate_fold

        fold = {
            "train_start": datetime(2026, 5, 1),
            "train_end": datetime(2026, 5, 8),
            "test_end": datetime(2026, 5, 11),
            "train": [_make_entry(70, "win", hours_ago=0)] * 5,  # 5 < MIN_TRAIN_OBS=20
            "test": [_make_entry(70, "win", hours_ago=0)] * 10,
        }
        self.assertIsNone(evaluate_fold(fold))

    def test_insufficient_test_returns_none(self) -> None:
        from core.walk_forward import evaluate_fold

        fold = {
            "train_start": datetime(2026, 5, 1),
            "train_end": datetime(2026, 5, 8),
            "test_end": datetime(2026, 5, 11),
            "train": [_make_entry(70, "win", hours_ago=0)] * 25,
            "test": [_make_entry(70, "win", hours_ago=0)] * 2,  # 2 < MIN_TEST_OBS=5
        }
        self.assertIsNone(evaluate_fold(fold))

    def test_returns_metrics_for_valid_fold(self) -> None:
        from core.walk_forward import evaluate_fold

        # train: 25 точек, mix win/loss; test: 10 точек.
        train = [
            _make_entry(70, "win" if i % 2 == 0 else "loss", hours_ago=0)
            for i in range(25)
        ]
        test = [
            _make_entry(70, "win" if i % 3 == 0 else "loss", hours_ago=0)
            for i in range(10)
        ]

        fold = {
            "train_start": datetime(2026, 5, 1),
            "train_end": datetime(2026, 5, 8),
            "test_end": datetime(2026, 5, 11),
            "train": train,
            "test": test,
        }
        result = evaluate_fold(fold)

        self.assertIsNotNone(result)
        for k in (
            "raw_oos_brier",
            "calibrated_oos_brier",
            "brier_improvement",
            "n_train",
            "n_test",
        ):
            self.assertIn(k, result)
        self.assertEqual(result["n_train"], 25)
        self.assertEqual(result["n_test"], 10)


class AggregateFoldsTestCase(unittest.TestCase):
    def test_empty_yields_insufficient_data(self) -> None:
        from core.walk_forward import aggregate_folds

        agg = aggregate_folds([])
        self.assertEqual(agg["verdict"], "INSUFFICIENT_DATA")
        self.assertEqual(agg["n_folds"], 0)

    def test_single_fold_insufficient(self) -> None:
        from core.walk_forward import aggregate_folds

        agg = aggregate_folds([{"raw_oos_brier": 0.25, "calibrated_oos_brier": 0.20,
                                "brier_improvement": 0.05}])
        self.assertEqual(agg["verdict"], "INSUFFICIENT_DATA")

    def test_calibration_helps_when_most_folds_improve(self) -> None:
        from core.walk_forward import aggregate_folds

        # 4 фолда из 5 показали улучшение
        folds = [
            {"raw_oos_brier": 0.25, "calibrated_oos_brier": 0.20, "brier_improvement": 0.05},
            {"raw_oos_brier": 0.30, "calibrated_oos_brier": 0.22, "brier_improvement": 0.08},
            {"raw_oos_brier": 0.22, "calibrated_oos_brier": 0.18, "brier_improvement": 0.04},
            {"raw_oos_brier": 0.27, "calibrated_oos_brier": 0.21, "brier_improvement": 0.06},
            {"raw_oos_brier": 0.20, "calibrated_oos_brier": 0.21, "brier_improvement": -0.01},
        ]
        agg = aggregate_folds(folds)
        self.assertEqual(agg["verdict"], "CALIBRATION_HELPS")
        self.assertEqual(agg["n_folds"], 5)
        self.assertEqual(agg["n_folds_with_improvement"], 4)
        self.assertGreater(agg["absolute_improvement"], 0)

    def test_raw_better_when_most_folds_worse(self) -> None:
        from core.walk_forward import aggregate_folds

        folds = [
            {"raw_oos_brier": 0.20, "calibrated_oos_brier": 0.25, "brier_improvement": -0.05},
            {"raw_oos_brier": 0.22, "calibrated_oos_brier": 0.30, "brier_improvement": -0.08},
            {"raw_oos_brier": 0.18, "calibrated_oos_brier": 0.22, "brier_improvement": -0.04},
        ]
        agg = aggregate_folds(folds)
        self.assertEqual(agg["verdict"], "RAW_BETTER")
        self.assertEqual(agg["n_folds_with_improvement"], 0)
        self.assertLess(agg["absolute_improvement"], 0)


class FormatBacktestTelegramTestCase(unittest.TestCase):
    def test_insufficient_data_renders(self) -> None:
        from core.walk_forward import format_backtest_telegram

        result = {
            "config": {
                "window_days": 30,
                "train_days": 14,
                "test_days": 7,
                "step_days": 7,
                "asset": None,
            },
            "n_resolved": 5,
            "folds": [],
            "aggregate": {
                "verdict": "INSUFFICIENT_DATA",
                "n_folds": 0,
                "raw_oos_brier_mean": None,
                "calibrated_oos_brier_mean": None,
                "absolute_improvement": None,
                "relative_improvement_pct": None,
                "n_folds_with_improvement": 0,
            },
            "verdict": "INSUFFICIENT_DATA",
        }
        rendered = format_backtest_telegram(result)
        self.assertIn("Walk-forward", rendered)
        self.assertIn("Недостаточно данных", rendered)

    def test_helps_verdict_renders(self) -> None:
        from core.walk_forward import format_backtest_telegram

        result = {
            "config": {
                "window_days": 60,
                "train_days": 14,
                "test_days": 7,
                "step_days": 7,
                "asset": "BTC",
            },
            "n_resolved": 80,
            "folds": [{"raw_oos_brier": 0.25, "calibrated_oos_brier": 0.20,
                       "brier_improvement": 0.05}] * 4,
            "aggregate": {
                "verdict": "CALIBRATION_HELPS",
                "n_folds": 4,
                "raw_oos_brier_mean": 0.25,
                "calibrated_oos_brier_mean": 0.20,
                "absolute_improvement": 0.05,
                "relative_improvement_pct": 20.0,
                "n_folds_with_improvement": 4,
            },
            "verdict": "CALIBRATION_HELPS",
        }
        rendered = format_backtest_telegram(result)
        self.assertIn("BTC", rendered)
        self.assertIn("Калибровка помогает", rendered)


# ─── Async end-to-end (walk_forward_backtest с реальным DB) ──────────────────


class WalkForwardBacktestTestCase(unittest.IsolatedAsyncioTestCase):
    """End-to-end: пишем provenance + predictions в temp SQLite,
    запускаем walk_forward_backtest, проверяем что отдаёт согласованный verdict."""

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="wf_test_")
        self._db_path = os.path.join(self._tmpdir, "test_wf.db")

        self._patches = [
            patch("config.DB_PATH", self._db_path),
            patch("core.provenance.DB_PATH", self._db_path),
            patch("core.calibration.DB_PATH", self._db_path),
        ]
        for p in self._patches:
            p.start()

        from core import provenance
        await provenance.ensure_table()

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

    async def _seed_decision(
        self,
        days_ago: int,
        asset: str,
        score: int,
        result: str,
        pnl_pct: float = 0.0,
    ) -> None:
        """Вставляет prediction + provenance с явным created_at смещением."""
        import aiosqlite

        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                """
                INSERT INTO predictions
                    (asset, direction, entry_price, target_price, stop_loss,
                     timeframe, source_news, result, pnl_pct, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', ?))
                """,
                (asset, "LONG", 100.0, 110.0, 95.0, "1D", "test",
                 result, pnl_pct, f"-{days_ago} days"),
            )
            pred_id = int(cur.lastrowid)

            # Provenance с явным created_at смещением + linked prediction_id.
            await db.execute(
                """
                INSERT INTO decision_provenance (
                    created_at, decision_type, asset, direction, score,
                    features_json, weights_json,
                    code_version, schema_version, prediction_id
                ) VALUES (
                    datetime('now', ?), 'signal_scorer', ?, 'LONG', ?,
                    '{}', '{"trend_alignment": 30, "total": 70}',
                    'test_v1', '1.0', ?
                )
                """,
                (f"-{days_ago} days", asset, score, pred_id),
            )
            await db.commit()

    async def test_insufficient_data_returns_safe_verdict(self) -> None:
        """3 решения — мало для backtest. Должен вернуть INSUFFICIENT_DATA."""
        from core.walk_forward import walk_forward_backtest

        for i in range(3):
            await self._seed_decision(days_ago=i + 1, asset="BTC", score=70, result="win")

        result = await walk_forward_backtest(
            window_days=30, train_days=7, test_days=3, step_days=3
        )
        self.assertEqual(result["verdict"], "INSUFFICIENT_DATA")

    async def test_end_to_end_calibration_overconfident(self) -> None:
        """40 решений, score=90 (overconfident), реализованный hit-rate ~50%.

        Должен пройти весь pipeline и вернуть рабочий agg dict.
        """
        from core.walk_forward import walk_forward_backtest

        # 40 решений разнесены по последним 28 дням.
        for i in range(40):
            days_ago = 28 - (i * 28 // 40)  # 28 ... 0
            # Чередуем win/loss → 50% hit-rate.
            result = "win" if i % 2 == 0 else "loss"
            await self._seed_decision(
                days_ago=days_ago,
                asset="BTC",
                score=90,
                result=result,
                pnl_pct=2.0 if result == "win" else -1.5,
            )

        result = await walk_forward_backtest(
            window_days=30, train_days=10, test_days=5, step_days=5
        )

        self.assertIn(result["verdict"], ("CALIBRATION_HELPS", "RAW_BETTER", "INSUFFICIENT_DATA"))
        self.assertGreater(result["n_resolved"], 30)


if __name__ == "__main__":
    unittest.main()
