"""Unit-тесты для core/post_mortem.py.

Покрывают:
  • normalize_direction — синонимы LONG / BUY / 🐂 / БЫЧ → BULLISH.
  • classify_outcome — все 6 веток (hit, miss, flat, neutral_correct,
    neutral_missed, no_data) + граничные значения.
  • outcome_to_prediction_result — маппинг в формат predictions.result.
  • explain_call — человекочитаемая строка с эмоджи + score_breakdown.
  • compute_stats — counts, hit_rate, граничные случаи (0 / divide-zero).
  • pick_target_digest — выбирает самый свежий ≥24ч + target_date точное
    совпадение.
  • evaluate_digest_forecasts — конец-в-конец, fixture с DigestParser.
  • write_outcomes_back — пишет в predictions + линкует provenance.
  • format_telegram / format_markdown — рендеры.

Все тесты без сети: PriceFetcher injectable через `historical_fn` /
`current_fn` параметры.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch


class ClassifyOutcomeTestCase(unittest.TestCase):
    """Чистые функции — никакой БД / сети."""

    def test_bullish_hit(self) -> None:
        from core.post_mortem import classify_outcome
        self.assertEqual(classify_outcome("BULLISH", 2.0), "hit")
        self.assertEqual(classify_outcome("LONG", 0.6), "hit")
        self.assertEqual(classify_outcome("🐂", 1.0), "hit")
        self.assertEqual(classify_outcome("БЫЧЬИЙ", 5.0), "hit")

    def test_bullish_miss(self) -> None:
        from core.post_mortem import classify_outcome
        self.assertEqual(classify_outcome("BULLISH", -2.0), "miss")
        self.assertEqual(classify_outcome("LONG", -10.0), "miss")

    def test_bearish_hit(self) -> None:
        from core.post_mortem import classify_outcome
        self.assertEqual(classify_outcome("BEARISH", -2.0), "hit")
        self.assertEqual(classify_outcome("SHORT", -0.6), "hit")

    def test_bearish_miss(self) -> None:
        from core.post_mortem import classify_outcome
        self.assertEqual(classify_outcome("BEARISH", 2.0), "miss")

    def test_flat_below_threshold(self) -> None:
        """|return| ≤ 0.5% при non-neutral direction → flat."""
        from core.post_mortem import classify_outcome
        self.assertEqual(classify_outcome("BULLISH", 0.3), "flat")
        self.assertEqual(classify_outcome("BEARISH", -0.4), "flat")
        # Точно граница — 0.5% = flat (≤).
        self.assertEqual(classify_outcome("BULLISH", 0.5), "flat")

    def test_neutral_correct(self) -> None:
        from core.post_mortem import classify_outcome
        self.assertEqual(classify_outcome("NEUTRAL", 0.3), "neutral_correct")
        self.assertEqual(classify_outcome("НЕЙТРАЛЬНЫЙ", -0.2), "neutral_correct")

    def test_neutral_missed(self) -> None:
        from core.post_mortem import classify_outcome
        self.assertEqual(classify_outcome("NEUTRAL", 5.0), "neutral_missed")
        self.assertEqual(classify_outcome("NEUTRAL", -5.0), "neutral_missed")

    def test_no_data(self) -> None:
        from core.post_mortem import classify_outcome
        self.assertEqual(classify_outcome("BULLISH", None), "no_data")
        self.assertEqual(classify_outcome("NEUTRAL", None), "no_data")

    def test_custom_threshold(self) -> None:
        """Если кто-то ужесточит порог — flat расширяется."""
        from core.post_mortem import classify_outcome
        self.assertEqual(
            classify_outcome("BULLISH", 0.9, flat_threshold=1.0),
            "flat",
        )
        self.assertEqual(
            classify_outcome("BULLISH", 1.1, flat_threshold=1.0),
            "hit",
        )


class NormalizeDirectionTestCase(unittest.TestCase):
    def test_bullish_synonyms(self) -> None:
        from core.post_mortem import normalize_direction
        for tok in ("BULLISH", "Bull", "long", "BUY", "🐂", "БЫЧИЙ"):
            self.assertEqual(normalize_direction(tok), "BULLISH", tok)

    def test_bearish_synonyms(self) -> None:
        from core.post_mortem import normalize_direction
        for tok in ("BEARISH", "Bear", "short", "SELL", "🐻", "МЕДВЕЖИЙ"):
            self.assertEqual(normalize_direction(tok), "BEARISH", tok)

    def test_neutral_or_unknown(self) -> None:
        from core.post_mortem import normalize_direction
        self.assertEqual(normalize_direction("NEUTRAL"), "NEUTRAL")
        self.assertEqual(normalize_direction("Нейтральный"), "NEUTRAL")
        # Неизвестные строки → NEUTRAL по дефолту (consveрвативно).
        self.assertEqual(normalize_direction(""), "NEUTRAL")
        self.assertEqual(normalize_direction("???"), "NEUTRAL")

    def test_canonical_long_short(self) -> None:
        from core.post_mortem import direction_to_canonical_long_short
        self.assertEqual(direction_to_canonical_long_short("BULLISH"), "LONG")
        self.assertEqual(direction_to_canonical_long_short("LONG"), "LONG")
        self.assertEqual(direction_to_canonical_long_short("BEARISH"), "SHORT")
        self.assertEqual(direction_to_canonical_long_short("NEUTRAL"), "NONE")


class OutcomeToPredictionResultTestCase(unittest.TestCase):
    def test_hits_become_win(self) -> None:
        from core.post_mortem import outcome_to_prediction_result
        self.assertEqual(outcome_to_prediction_result("hit"), "win")
        self.assertEqual(outcome_to_prediction_result("neutral_correct"), "win")

    def test_misses_become_loss(self) -> None:
        from core.post_mortem import outcome_to_prediction_result
        self.assertEqual(outcome_to_prediction_result("miss"), "loss")
        self.assertEqual(outcome_to_prediction_result("neutral_missed"), "loss")

    def test_flat_becomes_caution(self) -> None:
        from core.post_mortem import outcome_to_prediction_result
        self.assertEqual(outcome_to_prediction_result("flat"), "caution")

    def test_no_data_becomes_expired(self) -> None:
        from core.post_mortem import outcome_to_prediction_result
        self.assertEqual(outcome_to_prediction_result("no_data"), "expired")


class ExplainCallTestCase(unittest.TestCase):
    def test_basic_explanation(self) -> None:
        from core.post_mortem import explain_call
        text = explain_call("BTC", "BULLISH", 2.5, "hit")
        self.assertIn("BTC", text)
        self.assertIn("bullish", text)
        self.assertIn("+2.50%", text)
        self.assertIn("Верно", text)

    def test_miss_negative_label(self) -> None:
        from core.post_mortem import explain_call
        text = explain_call("ETH", "BEARISH", 3.1, "miss")
        self.assertIn("Неверно", text)
        # Должно упомянуть калибровку с negative label.
        self.assertIn("negative-label", text.lower().replace("ё", "e"))

    def test_no_data_keeps_dash(self) -> None:
        from core.post_mortem import explain_call
        text = explain_call("SOL", "BULLISH", None, "no_data")
        self.assertIn("—", text)

    def test_score_breakdown_top_driver(self) -> None:
        from core.post_mortem import explain_call
        breakdown = {"trend_alignment": 25, "rsi_oversold": -8, "vrt": 5}
        text = explain_call("BTC", "BULLISH", 2.0, "hit", score_breakdown=breakdown)
        # 25 — самый большой по abs, должен попасть в "главный драйвер".
        self.assertIn("trend_alignment", text)
        self.assertIn("25", text)


class ComputeStatsTestCase(unittest.TestCase):
    def _make_entry(self, outcome: str):
        from core.post_mortem import PostMortemEntry
        return PostMortemEntry(
            asset="BTC",
            direction="BULLISH",
            forecast_date="18.05.2026",
            entry_price=100.0,
            eval_price=102.0,
            return_pct=2.0,
            outcome=outcome,
            explanation="",
        )

    def test_basic_counts(self) -> None:
        from core.post_mortem import compute_stats
        entries = [
            self._make_entry("hit"),
            self._make_entry("hit"),
            self._make_entry("miss"),
            self._make_entry("flat"),
            self._make_entry("neutral_correct"),
            self._make_entry("neutral_missed"),
            self._make_entry("no_data"),
        ]
        s = compute_stats(entries)
        self.assertEqual(s["total"], 7)
        self.assertEqual(s["knowable"], 6)  # всё кроме no_data
        # resolved = hit + miss + neutral_correct + neutral_missed = 2+1+1+1 = 5
        # (flat и no_data исключены — рынок не дал чёткого ответа)
        self.assertEqual(s["resolved"], 5)
        self.assertEqual(s["wins"], 3)  # 2 hit + 1 neutral_correct
        self.assertEqual(s["losses"], 2)  # 1 miss + 1 neutral_missed
        self.assertEqual(s["flat"], 1)
        self.assertEqual(s["no_data"], 1)
        self.assertAlmostEqual(s["hit_rate"], 3 / 5, places=4)

    def test_hit_rate_none_when_no_resolved(self) -> None:
        from core.post_mortem import compute_stats
        entries = [self._make_entry("no_data"), self._make_entry("flat")]
        s = compute_stats(entries)
        self.assertIsNone(s["hit_rate"])
        self.assertEqual(s["resolved"], 0)

    def test_empty_list(self) -> None:
        from core.post_mortem import compute_stats
        s = compute_stats([])
        self.assertEqual(s["total"], 0)
        self.assertIsNone(s["hit_rate"])


class PickTargetDigestTestCase(unittest.TestCase):
    def _make_digest(self, date: str) -> dict:
        return {"date": date, "section": ""}

    def test_picks_latest_old_enough(self) -> None:
        from core.post_mortem import pick_target_digest
        # 19.05.2026 12:00 — "сейчас".  18 — старый, 19-утро — слишком свежий.
        now = datetime(2026, 5, 19, 12, 0)
        digests = [
            self._make_digest("17.05.2026 09:00"),
            self._make_digest("18.05.2026 09:00"),
            self._make_digest("19.05.2026 09:00"),  # < 24h назад
        ]
        result = pick_target_digest(digests, now=now)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["date"], "18.05.2026 09:00")

    def test_target_date_exact(self) -> None:
        from core.post_mortem import pick_target_digest
        digests = [
            self._make_digest("17.05.2026 09:00"),
            self._make_digest("18.05.2026 09:00"),
        ]
        result = pick_target_digest(digests, target_date="17.05.2026")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["date"], "17.05.2026 09:00")

    def test_target_date_no_match(self) -> None:
        from core.post_mortem import pick_target_digest
        digests = [self._make_digest("18.05.2026 09:00")]
        result = pick_target_digest(digests, target_date="01.01.2020")
        self.assertIsNone(result)

    def test_empty_list(self) -> None:
        from core.post_mortem import pick_target_digest
        self.assertIsNone(pick_target_digest([]))

    def test_all_too_recent(self) -> None:
        """Все дайджесты <24ч → вернёт None."""
        from core.post_mortem import pick_target_digest
        now = datetime(2026, 5, 19, 12, 0)
        digests = [self._make_digest("19.05.2026 09:00")]
        self.assertIsNone(pick_target_digest(digests, now=now))


class EvaluateDigestForecastsTestCase(unittest.IsolatedAsyncioTestCase):
    """End-to-end оценка набора forecast-dict'ов с фейковыми ценами."""

    async def test_evaluates_direction_forecasts(self) -> None:
        from core.post_mortem import evaluate_digest_forecasts

        forecasts = [
            {
                "asset": "BTC",
                "forecast": "BULLISH",
                "forecast_type": "direction",
                "date": "18.05.2026 09:00",
            },
            {
                "asset": "ETH",
                "forecast": "BEARISH",
                "forecast_type": "direction",
                "date": "18.05.2026 09:00",
            },
            {
                "asset": "SOL",
                "forecast": "NEUTRAL",
                "forecast_type": "direction",
                "date": "18.05.2026 09:00",
            },
            # price-forecast — пропускается.
            {
                "asset": "BTC",
                "forecast": "$80000",
                "forecast_type": "price",
                "date": "18.05.2026 09:00",
            },
        ]

        prices = {
            "BTC-USD": (78000.0, 80500.0),   # +3.2% → BTC BULLISH hits
            "ETH-USD": (2100.0, 2050.0),     # -2.4% → ETH BEARISH hits
            "SOL-USD": (150.0, 150.4),       # +0.27% → SOL NEUTRAL correct
        }

        async def hist(t: str, d: str):
            return prices[t][0]

        async def cur(t: str):
            return prices[t][1]

        entries = await evaluate_digest_forecasts(forecasts, hist, cur)
        self.assertEqual(len(entries), 3)  # price-forecast skipped

        by_asset = {e.asset: e for e in entries}
        self.assertEqual(by_asset["BTC"].outcome, "hit")
        self.assertEqual(by_asset["ETH"].outcome, "hit")
        self.assertEqual(by_asset["SOL"].outcome, "neutral_correct")

    async def test_verdict_uses_btc_proxy(self) -> None:
        from core.post_mortem import evaluate_digest_forecasts

        forecasts = [
            {
                "asset": "VERDICT",
                "forecast": "BULLISH",
                "forecast_type": "direction",
                "date": "18.05.2026 09:00",
            },
        ]

        async def hist(t: str, d: str):
            self.assertEqual(t, "BTC-USD")  # VERDICT мапится в BTC-USD
            return 78000.0

        async def cur(t: str):
            return 80000.0

        entries = await evaluate_digest_forecasts(forecasts, hist, cur)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].asset, "VERDICT")
        self.assertEqual(entries[0].outcome, "hit")

    async def test_handles_missing_prices(self) -> None:
        from core.post_mortem import evaluate_digest_forecasts

        forecasts = [
            {
                "asset": "BTC",
                "forecast": "BULLISH",
                "forecast_type": "direction",
                "date": "18.05.2026 09:00",
            },
        ]

        async def hist(t: str, d: str):
            return None

        async def cur(t: str):
            return None

        entries = await evaluate_digest_forecasts(forecasts, hist, cur)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].outcome, "no_data")
        self.assertIsNone(entries[0].return_pct)

    async def test_skips_assets_without_yahoo_mapping(self) -> None:
        """Fear&Greed (ASSET_TO_YAHOO=None) → entry_price=None → no_data."""
        from core.post_mortem import evaluate_digest_forecasts

        forecasts = [
            {
                "asset": "Fear&Greed",
                "forecast": "BULLISH",
                "forecast_type": "direction",
                "date": "18.05.2026 09:00",
            },
        ]

        async def hist(t: str, d: str):  # не должен вызываться
            raise AssertionError("hist must not be called for Fear&Greed")

        async def cur(t: str):
            raise AssertionError("cur must not be called for Fear&Greed")

        entries = await evaluate_digest_forecasts(forecasts, hist, cur)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].outcome, "no_data")


class WriteOutcomesBackTestCase(unittest.IsolatedAsyncioTestCase):
    """Проверяем что write_outcomes_back пишет в predictions + линкует provenance."""

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="post_mortem_test_")
        self._db_path = os.path.join(self._tmpdir, "test_pm.db")

        # Создаём минимальные таблицы predictions + decision_provenance.
        conn = sqlite3.connect(self._db_path)
        conn.executescript(
            """
            CREATE TABLE predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                asset TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                target_price REAL,
                stop_loss REAL,
                timeframe TEXT,
                source_news TEXT,
                result TEXT,
                result_price REAL,
                result_at TEXT,
                pnl_pct REAL,
                prediction_type TEXT NOT NULL DEFAULT 'long_term',
                forecast TEXT,
                fact TEXT,
                report_type TEXT NOT NULL DEFAULT 'global'
            );
            CREATE TABLE decision_provenance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                decision_type TEXT NOT NULL,
                asset TEXT NOT NULL,
                direction TEXT NOT NULL,
                score INTEGER,
                entry_price REAL,
                stop_loss REAL,
                take_profit REAL,
                sigma_1d_pct REAL,
                features_json TEXT NOT NULL,
                weights_json TEXT NOT NULL,
                signals_json TEXT,
                regime_json TEXT,
                code_version TEXT,
                schema_version TEXT NOT NULL DEFAULT '1.0',
                prediction_id INTEGER,
                trade_log_id INTEGER
            );
            """
        )
        conn.commit()
        conn.close()

        self._patches = [
            patch("config.DB_PATH", self._db_path),
            patch("core.post_mortem.DB_PATH", self._db_path),
        ]
        for p in self._patches:
            p.start()

    async def asyncTearDown(self) -> None:
        for p in self._patches:
            p.stop()
        try:
            os.remove(self._db_path)
            os.rmdir(self._tmpdir)
        except OSError:
            pass

    async def test_writes_predictions(self) -> None:
        from core.post_mortem import PostMortemEntry, write_outcomes_back

        entries = [
            PostMortemEntry(
                asset="BTC",
                direction="BULLISH",
                forecast_date="18.05.2026 09:00",
                entry_price=78000.0,
                eval_price=80500.0,
                return_pct=3.21,
                outcome="hit",
                explanation="ok",
            ),
            PostMortemEntry(
                asset="ETH",
                direction="BEARISH",
                forecast_date="18.05.2026 09:00",
                entry_price=2100.0,
                eval_price=2150.0,
                return_pct=2.38,
                outcome="miss",
                explanation="ok",
            ),
            # no_data — НЕ пишется.
            PostMortemEntry(
                asset="Fear&Greed",
                direction="BULLISH",
                forecast_date="18.05.2026 09:00",
                entry_price=None,
                eval_price=None,
                return_pct=None,
                outcome="no_data",
                explanation="no price",
            ),
        ]

        written = await write_outcomes_back(entries, db_path=self._db_path)
        self.assertEqual(written, 2)

        conn = sqlite3.connect(self._db_path)
        rows = conn.execute(
            "SELECT asset, direction, result, pnl_pct, prediction_type, report_type "
            "FROM predictions ORDER BY asset"
        ).fetchall()
        conn.close()

        self.assertEqual(len(rows), 2)
        # BTC hit → win
        self.assertEqual(rows[0], ("BTC", "LONG", "win", 3.21, "daily_digest", "post_mortem"))
        # ETH miss → loss
        self.assertEqual(rows[1], ("ETH", "SHORT", "loss", 2.38, "daily_digest", "post_mortem"))

        # entries обогатились prediction_id
        self.assertIsNotNone(entries[0].prediction_id)
        self.assertIsNotNone(entries[1].prediction_id)
        self.assertIsNone(entries[2].prediction_id)  # no_data

    async def test_links_provenance_when_match(self) -> None:
        """Если есть unlinked provenance с тем же asset+direction в окне ±2ч —
        write_outcomes_back должен заполнить decision_provenance.prediction_id."""
        from core.post_mortem import PostMortemEntry, write_outcomes_back

        # Создаём provenance запись на 18.05.2026 10:00 (через 1ч после digest).
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            """
            INSERT INTO decision_provenance
            (created_at, decision_type, asset, direction, score,
             entry_price, stop_loss, take_profit, sigma_1d_pct,
             features_json, weights_json)
            VALUES ('2026-05-18 10:00:00', 'signal_scorer', 'BTC', 'LONG', 65,
                    78100, 76000, 82000, 1.5, '{}', '{}')
            """
        )
        conn.commit()
        conn.close()

        entries = [
            PostMortemEntry(
                asset="BTC",
                direction="BULLISH",
                forecast_date="18.05.2026 09:00",
                entry_price=78000.0,
                eval_price=80500.0,
                return_pct=3.21,
                outcome="hit",
                explanation="ok",
            ),
        ]
        await write_outcomes_back(entries, db_path=self._db_path)

        conn = sqlite3.connect(self._db_path)
        row = conn.execute(
            "SELECT prediction_id FROM decision_provenance LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row[0])
        self.assertEqual(entries[0].provenance_id, 1)

    async def test_does_not_link_provenance_outside_window(self) -> None:
        """Provenance в 5ч от digest — слишком далеко (window=2ч)."""
        from core.post_mortem import PostMortemEntry, write_outcomes_back

        conn = sqlite3.connect(self._db_path)
        conn.execute(
            """
            INSERT INTO decision_provenance
            (created_at, decision_type, asset, direction, score,
             entry_price, stop_loss, take_profit, sigma_1d_pct,
             features_json, weights_json)
            VALUES ('2026-05-18 15:00:00', 'signal_scorer', 'BTC', 'LONG', 65,
                    78100, 76000, 82000, 1.5, '{}', '{}')
            """
        )
        conn.commit()
        conn.close()

        entries = [
            PostMortemEntry(
                asset="BTC",
                direction="BULLISH",
                forecast_date="18.05.2026 09:00",
                entry_price=78000.0,
                eval_price=80500.0,
                return_pct=3.21,
                outcome="hit",
                explanation="ok",
            ),
        ]
        await write_outcomes_back(entries, db_path=self._db_path)

        conn = sqlite3.connect(self._db_path)
        row = conn.execute(
            "SELECT prediction_id FROM decision_provenance LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertIsNone(row[0])  # не залинковали
        self.assertIsNone(entries[0].provenance_id)


class FormatTelegramTestCase(unittest.TestCase):
    def test_renders_basic_report(self) -> None:
        from core.post_mortem import PostMortemEntry, PostMortemReport, format_telegram

        report = PostMortemReport(
            digest_date="18.05.2026 09:00",
            horizon_hours=24,
            entries=[
                PostMortemEntry(
                    asset="BTC", direction="BULLISH",
                    forecast_date="18.05.2026", entry_price=78000.0,
                    eval_price=80500.0, return_pct=3.2,
                    outcome="hit", explanation="ok",
                ),
                PostMortemEntry(
                    asset="ETH", direction="BEARISH",
                    forecast_date="18.05.2026", entry_price=2100.0,
                    eval_price=2150.0, return_pct=2.4,
                    outcome="miss", explanation="bad",
                ),
            ],
        )
        text = format_telegram(report)
        self.assertIn("18.05.2026", text)
        self.assertIn("Hit-rate", text)
        # 1 hit из 2 resolved = 50%
        self.assertIn("50.0%", text)
        self.assertIn("BTC", text)
        self.assertIn("ETH", text)

    def test_empty_report(self) -> None:
        from core.post_mortem import PostMortemReport, format_telegram

        report = PostMortemReport(digest_date="18.05.2026", horizon_hours=24, entries=[])
        text = format_telegram(report)
        self.assertIn("не нашёл direction-прогнозов", text)


class FormatMarkdownTestCase(unittest.TestCase):
    def test_renders_markdown_table(self) -> None:
        from core.post_mortem import PostMortemEntry, PostMortemReport, format_markdown

        report = PostMortemReport(
            digest_date="18.05.2026",
            horizon_hours=24,
            entries=[
                PostMortemEntry(
                    asset="BTC", direction="BULLISH",
                    forecast_date="18.05.2026", entry_price=78000.0,
                    eval_price=80500.0, return_pct=3.2,
                    outcome="hit", explanation="ok",
                ),
            ],
        )
        md = format_markdown(report)
        self.assertIn("Post-mortem", md)
        self.assertIn("BTC", md)
        self.assertIn("BULLISH", md)
        self.assertIn("+3.20%", md)
        self.assertIn("| Asset | Direction | Entry | Eval | Δ% | Outcome | Объяснение |", md)


if __name__ == "__main__":
    unittest.main()
