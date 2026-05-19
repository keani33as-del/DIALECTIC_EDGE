"""Unit-тесты для core/provenance.py.

Покрывают:
  • freeze_scorer_decision() — пишет в таблицу, возвращает id, поля
    корректно десериализуются обратно.
  • freeze_pick_best_decision() — пишет даже когда best=None (записывает
    NONE-решение, чтобы потом понять «почему ничего не выбрали»).
  • get_provenance() — возвращает dict с десериализованными JSON-полями.
  • get_recent_provenances() — фильтрация по asset / decision_type.
  • _extract_regime / _compact_binance / _compact_bias_map — helpers.
  • Идемпотентность: две последовательные заморозки → две разные записи
    (provenance НЕ дедуплицирует — каждое решение пишется отдельно).
  • JSON truncation для огромных features.
  • format_provenance_telegram — рендер для UI.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch


class ProvenanceTestCase(unittest.IsolatedAsyncioTestCase):
    """Используем временную БД через config.DB_PATH override."""

    async def asyncSetUp(self) -> None:
        # Временный файл БД на тест-кейс. patch + reimport чтобы DB_PATH
        # подменился до того, как provenance.py его прочитает.
        self._tmpdir = tempfile.mkdtemp(prefix="prov_test_")
        self._db_path = os.path.join(self._tmpdir, "test_prov.db")

        # Патчим DB_PATH в обоих модулях (config + provenance).
        self._patches = [
            patch("config.DB_PATH", self._db_path),
            patch("core.provenance.DB_PATH", self._db_path),
        ]
        for p in self._patches:
            p.start()

        from core import provenance
        await provenance.ensure_table()

    async def asyncTearDown(self) -> None:
        for p in self._patches:
            p.stop()
        try:
            os.remove(self._db_path)
            os.rmdir(self._tmpdir)
        except OSError:
            pass

    # ─── freeze_scorer_decision ──────────────────────────────────────────────

    async def test_freeze_scorer_writes_all_fields(self) -> None:
        from core.provenance import freeze_scorer_decision, get_provenance

        prov_id = await freeze_scorer_decision(
            asset="SOL",
            direction="SHORT",
            score=72,
            entry_price=158.5,
            stop_loss=165.0,
            take_profit=145.0,
            sigma_1d_pct=3.20,
            features={
                "price": 158.5,
                "trend": "DOWNTREND",
                "ma50": 162.0,
                "ma200": 178.0,
                "complexity_hint": "MEAN_REVERTING",
                "hurst": 0.42,
                "markov_state": "DOWN",
                "vol_sigma_1d_pct": 3.20,
            },
            weights={
                "trend_alignment": 25,
                "complexity_hint": 15,
                "vrt_evidence": 10,
                "markov_pull": 12,
                "tradable_score": 10,
                "total": 72,
            },
        )

        self.assertGreater(prov_id, 0)
        loaded = await get_provenance(prov_id)
        self.assertIsNotNone(loaded)
        assert loaded is not None  # mypy
        self.assertEqual(loaded["asset"], "SOL")
        self.assertEqual(loaded["direction"], "SHORT")
        self.assertEqual(loaded["score"], 72)
        self.assertEqual(loaded["entry_price"], 158.5)
        self.assertEqual(loaded["stop_loss"], 165.0)
        self.assertEqual(loaded["take_profit"], 145.0)
        self.assertEqual(loaded["sigma_1d_pct"], 3.20)
        self.assertEqual(loaded["decision_type"], "signal_scorer")
        self.assertEqual(loaded["schema_version"], "1.0")
        # JSON-поля десериализованы обратно в dict:
        self.assertIsInstance(loaded["features_json"], dict)
        self.assertEqual(loaded["features_json"]["trend"], "DOWNTREND")
        self.assertIsInstance(loaded["weights_json"], dict)
        self.assertEqual(loaded["weights_json"]["total"], 72)

    async def test_freeze_scorer_extracts_regime(self) -> None:
        from core.provenance import freeze_scorer_decision, get_provenance

        prov_id = await freeze_scorer_decision(
            asset="BTC",
            direction="LONG",
            score=65,
            entry_price=78000.0,
            stop_loss=76000.0,
            take_profit=82000.0,
            sigma_1d_pct=1.5,
            features={
                "price": 78000.0,
                "trend": "UPTREND",
                "ma50": 75000.0,
                "ma200": 71000.0,
                "hurst": 0.58,
                "markov_state": "UP",
                "complexity_hint": "TRENDING",
                "vol_sigma_1d_pct": 1.5,
                "_irrelevant_key": "should_not_appear",
            },
            weights={"total": 65},
        )

        loaded = await get_provenance(prov_id)
        assert loaded is not None
        regime = loaded["regime_json"]
        self.assertIsInstance(regime, dict)
        self.assertEqual(regime.get("trend"), "UPTREND")
        self.assertEqual(regime.get("markov_state"), "UP")
        self.assertNotIn("_irrelevant_key", regime)

    async def test_freeze_scorer_explicit_regime_overrides_extraction(self) -> None:
        from core.provenance import freeze_scorer_decision, get_provenance

        prov_id = await freeze_scorer_decision(
            asset="ETH",
            direction="SHORT",
            score=55,
            entry_price=2100.0,
            stop_loss=2200.0,
            take_profit=1900.0,
            sigma_1d_pct=2.0,
            features={"price": 2100.0, "trend": "DOWNTREND"},
            weights={"total": 55},
            regime={"explicit_field": "custom_value", "regime": "RISK_OFF"},
        )
        loaded = await get_provenance(prov_id)
        assert loaded is not None
        self.assertEqual(loaded["regime_json"]["explicit_field"], "custom_value")
        self.assertEqual(loaded["regime_json"]["regime"], "RISK_OFF")
        # Trend из features НЕ попадает (т.к. regime передан явно):
        self.assertNotIn("trend", loaded["regime_json"])

    # ─── freeze_pick_best_decision ───────────────────────────────────────────

    async def test_freeze_pick_best_writes_signals_list(self) -> None:
        from core.provenance import freeze_pick_best_decision, get_provenance

        best = {
            "symbol": "SOLUSDT",
            "direction": "SHORT",
            "type": "BYBIT_TRADERS",
            "confidence": 65,
            "r_score": 78.5,
            "reason": "traders SHORT 12%",
            "r_score_components": {
                "confidence": 65, "bias_align": 12, "quant_confirm": 8,
            },
        }
        all_signals = [
            best,
            {"symbol": "BTCUSDT", "direction": "LONG", "type": "VERDICT_MATCH",
             "confidence": 50, "r_score": 60},
            {"symbol": "ETHUSDT", "direction": "SHORT", "type": "FUNDING",
             "confidence": 45, "r_score": 52},
        ]
        binance_data = {
            "SOLUSDT": {"last_price": 158.5, "price_change": -1.2,
                        "long": 30, "short": 70, "dominant": "SHORT",
                        "funding_rate": 0.0001, "quant_verdict": "SHORT",
                        "quant_confidence": 68},
            "BTCUSDT": {"last_price": 78000, "price_change": 0.5,
                        "long": 55, "short": 45},
        }
        verdict = {"verdict": "BEARISH"}
        bias_map = {
            "SOL": {"direction": "SHORT", "score": -14.0,
                    "quant_verdict": "SHORT", "quant_blocked": False},
            "BTC": {"direction": "NEUTRAL", "score": 2.0,
                    "quant_verdict": "NEUTRAL", "quant_blocked": False},
        }

        prov_id = await freeze_pick_best_decision(
            best, all_signals, binance_data, verdict, bias_map,
        )
        loaded = await get_provenance(prov_id)
        assert loaded is not None

        self.assertEqual(loaded["decision_type"], "pick_best")
        self.assertEqual(loaded["asset"], "SOL")
        self.assertEqual(loaded["direction"], "SHORT")
        self.assertEqual(loaded["score"], 78)
        # Список всех сигналов сохранён:
        self.assertIsInstance(loaded["signals_json"], list)
        self.assertEqual(len(loaded["signals_json"]), 3)
        symbols = {s["symbol"] for s in loaded["signals_json"]}
        self.assertEqual(symbols, {"SOLUSDT", "BTCUSDT", "ETHUSDT"})
        # Bias map сохранён в weights:
        self.assertIn("bias_map", loaded["weights_json"])
        self.assertEqual(
            loaded["weights_json"]["bias_map"]["SOL"]["direction"], "SHORT"
        )
        # verdict сохранён в features:
        self.assertEqual(loaded["features_json"]["verdict"]["verdict"], "BEARISH")

    async def test_freeze_pick_best_handles_none(self) -> None:
        """Когда pick_best не нашёл сигнала (None) — всё равно пишем NONE-запись.

        Это критично для понимания «почему ничего не выбрали» при ретроспективе.
        """
        from core.provenance import freeze_pick_best_decision, get_provenance

        prov_id = await freeze_pick_best_decision(
            best_signal=None,
            all_signals=[],
            binance_data={"BTCUSDT": {"last_price": 78000}},
            verdict=None,
        )
        loaded = await get_provenance(prov_id)
        assert loaded is not None
        self.assertEqual(loaded["asset"], "NONE")
        self.assertEqual(loaded["direction"], "NONE")
        self.assertEqual(loaded["score"], 0)
        self.assertEqual(loaded["signals_json"], [])

    # ─── get_recent_provenances ──────────────────────────────────────────────

    async def test_get_recent_provenances_filter(self) -> None:
        from core.provenance import freeze_scorer_decision, get_recent_provenances

        # Пишем три записи для разных активов:
        ids = []
        for asset, direction in [("BTC", "LONG"), ("ETH", "SHORT"), ("BTC", "SHORT")]:
            pid = await freeze_scorer_decision(
                asset=asset, direction=direction, score=60,
                entry_price=100.0, stop_loss=95.0, take_profit=110.0,
                sigma_1d_pct=2.0,
                features={"price": 100.0, "trend": "UPTREND"},
                weights={"total": 60},
            )
            ids.append(pid)

        all_recent = await get_recent_provenances(limit=10)
        self.assertEqual(len(all_recent), 3)

        only_btc = await get_recent_provenances(asset="BTC", limit=10)
        self.assertEqual(len(only_btc), 2)
        self.assertTrue(all(r["asset"] == "BTC" for r in only_btc))

        only_scorer = await get_recent_provenances(
            decision_type="signal_scorer", limit=10,
        )
        self.assertEqual(len(only_scorer), 3)

        only_pick_best = await get_recent_provenances(
            decision_type="pick_best", limit=10,
        )
        self.assertEqual(len(only_pick_best), 0)

    # ─── идемпотентность ─────────────────────────────────────────────────────

    async def test_two_freezes_create_two_records(self) -> None:
        """Provenance НЕ дедуплицирует — каждая заморозка = новая запись.

        Это by-design: даже идентичные решения дают разные snapshot'ы
        (разное время, возможно разная prices-кеш версия).
        """
        from core.provenance import freeze_scorer_decision, get_recent_provenances

        kwargs = dict(
            asset="BTC", direction="LONG", score=65,
            entry_price=78000.0, stop_loss=76000.0, take_profit=82000.0,
            sigma_1d_pct=1.5,
            features={"price": 78000.0, "trend": "UPTREND"},
            weights={"total": 65},
        )
        id1 = await freeze_scorer_decision(**kwargs)  # type: ignore[arg-type]
        id2 = await freeze_scorer_decision(**kwargs)  # type: ignore[arg-type]

        self.assertNotEqual(id1, id2)
        all_btc = await get_recent_provenances(asset="BTC", limit=10)
        self.assertEqual(len(all_btc), 2)

    # ─── link_prediction / link_trade_log ────────────────────────────────────

    async def test_link_prediction(self) -> None:
        from core.provenance import (
            freeze_scorer_decision, get_provenance, link_prediction,
        )

        prov_id = await freeze_scorer_decision(
            asset="BTC", direction="LONG", score=65,
            entry_price=78000.0, stop_loss=76000.0, take_profit=82000.0,
            sigma_1d_pct=1.5,
            features={"price": 78000.0},
            weights={"total": 65},
        )
        await link_prediction(prov_id, prediction_id=42)
        loaded = await get_provenance(prov_id)
        assert loaded is not None
        self.assertEqual(loaded["prediction_id"], 42)

    # ─── format_provenance_telegram ──────────────────────────────────────────

    async def test_format_for_telegram(self) -> None:
        from core.provenance import (
            format_provenance_telegram, freeze_scorer_decision, get_provenance,
        )

        prov_id = await freeze_scorer_decision(
            asset="SOL", direction="SHORT", score=72,
            entry_price=158.5, stop_loss=165.0, take_profit=145.0,
            sigma_1d_pct=3.2,
            features={"price": 158.5, "trend": "DOWNTREND"},
            weights={
                "trend_alignment": 25, "complexity_hint": 15,
                "vrt_evidence": 10, "markov_pull": 12,
                "tradable_score": 10, "total": 72,
            },
        )
        loaded = await get_provenance(prov_id)
        assert loaded is not None
        text = format_provenance_telegram(loaded)

        self.assertIn(f"#{prov_id}", text)
        self.assertIn("SOL", text)
        self.assertIn("SHORT", text)
        self.assertIn("72", text)
        # Все компоненты breakdown отрендерены:
        self.assertIn("trend_alignment", text)
        self.assertIn("complexity_hint", text)
        # Regime тоже:
        self.assertIn("Regime", text)

    # ─── safe_json truncation ────────────────────────────────────────────────

    async def test_safe_json_truncates_huge_features(self) -> None:
        from core.provenance import freeze_scorer_decision, get_provenance

        # 200KB features — гарантированно сверх _MAX_JSON_CHARS (64KB).
        huge_features = {
            "price": 100.0,
            "trend": "UPTREND",
            "huge_field": "x" * 200_000,
        }
        prov_id = await freeze_scorer_decision(
            asset="BTC", direction="LONG", score=60,
            entry_price=100.0, stop_loss=95.0, take_profit=110.0,
            sigma_1d_pct=2.0,
            features=huge_features,
            weights={"total": 60},
        )
        loaded = await get_provenance(prov_id)
        assert loaded is not None
        # Truncation flag (мы добавляем "…[truncated]" хвост) → JSON может
        # не парситься обратно как dict если truncated в середине string.
        # Главное — что вообще записалось без exception.
        raw = loaded["features_json"]
        if isinstance(raw, str):
            # Truncation сломала парсинг → значит точно truncated, ОК.
            self.assertIn("truncated", raw.lower())
        elif isinstance(raw, dict):
            # Если каким-то чудом распарсилось — длина в любом случае
            # значительно меньше исходных 200K.
            self.assertLess(len(str(raw)), 200_000)

    # ─── git_sha (best-effort) ───────────────────────────────────────────────

    async def test_code_version_present(self) -> None:
        from core.provenance import freeze_scorer_decision, get_provenance

        prov_id = await freeze_scorer_decision(
            asset="BTC", direction="LONG", score=60,
            entry_price=100.0, stop_loss=95.0, take_profit=110.0,
            sigma_1d_pct=2.0,
            features={"price": 100.0},
            weights={"total": 60},
        )
        loaded = await get_provenance(prov_id)
        assert loaded is not None
        # В CI .git может быть недоступен → разрешаем 'unknown',
        # но поле всё равно должно быть строкой.
        self.assertIsInstance(loaded["code_version"], str)
        self.assertGreater(len(loaded["code_version"]), 0)


class ProvenanceHelpersTestCase(unittest.TestCase):
    """Тесты для helper-функций (sync, без БД)."""

    def test_extract_regime_filters_keys(self) -> None:
        from core.provenance import _extract_regime

        features = {
            "price": 100.0,
            "trend": "UPTREND",
            "hurst": 0.55,
            "irrelevant": "noise",
            "_internal": "skip",
        }
        regime = _extract_regime(features)
        self.assertIn("price", regime)
        self.assertIn("trend", regime)
        self.assertIn("hurst", regime)
        self.assertNotIn("irrelevant", regime)
        self.assertNotIn("_internal", regime)

    def test_extract_regime_skips_none(self) -> None:
        from core.provenance import _extract_regime

        features = {
            "price": 100.0,
            "trend": None,  # должен быть отфильтрован
            "hurst": 0.55,
        }
        regime = _extract_regime(features)
        self.assertNotIn("trend", regime)
        self.assertIn("hurst", regime)

    def test_compact_binance_drops_heavy_fields(self) -> None:
        from core.provenance import _compact_binance

        binance_data = {
            "BTCUSDT": {
                "last_price": 78000,
                "price_change": 0.5,
                "funding_rate": 0.0001,
                "long": 55, "short": 45,
                "quant_components": {"atr": 1500, "bb_width": 3500},  # heavy
                "raw_orderbook": [1, 2, 3, 4, 5] * 1000,  # heavy
            },
            "not_a_dict": "skip me",
        }
        compact = _compact_binance(binance_data)
        self.assertIn("BTCUSDT", compact)
        self.assertNotIn("not_a_dict", compact)
        self.assertEqual(compact["BTCUSDT"]["last_price"], 78000)
        # Тяжёлые поля выкинуты:
        self.assertNotIn("quant_components", compact["BTCUSDT"])
        self.assertNotIn("raw_orderbook", compact["BTCUSDT"])

    def test_compact_bias_map_handles_dataclass_like(self) -> None:
        from core.provenance import _compact_bias_map

        class FakeBias:
            def __init__(self):
                self.direction = "LONG"
                self.score = 12.5
                self._private = "skip"

        bias_map = {
            "BTC": {"direction": "SHORT", "score": -8.0},  # dict
            "ETH": FakeBias(),                              # object
            "SOL": "string_value",                          # fallback to str
        }
        compact = _compact_bias_map(bias_map)
        self.assertEqual(compact["BTC"]["direction"], "SHORT")
        self.assertEqual(compact["ETH"]["direction"], "LONG")
        self.assertNotIn("_private", compact["ETH"])
        self.assertEqual(compact["SOL"], "string_value")

    def test_safe_json_handles_unserializable(self) -> None:
        from core.provenance import _safe_json

        class Unserializable:
            def __repr__(self) -> str:
                return "Unserializable()"

        # Не-JSON-serializable объект → default=str превращает в строку.
        raw = _safe_json({"obj": Unserializable()})
        self.assertIn("Unserializable", raw)


if __name__ == "__main__":
    unittest.main()
