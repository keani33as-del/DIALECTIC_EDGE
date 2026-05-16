"""Регресс-тесты для anti-whiplash гвардов в signal_trader.

Что ловим (1.5 месяца наблюдений на проде):
  • Вход → через 60 сек выход с PnL ≈ 0 по тому же signal_bias.
  • Контр-трендовый «buy the dip» в сильном downtrend.
  • Reset капитала на $100 вместо «учебных» $500.

Тестируем:
  1. _reentry_blocked / _arm_reentry_cooldown — корректный cooldown.
  2. _position_age_minutes — корректный парсинг created_at / entry_ts.
  3. _close_on_signal_reversal —
        a. min hold time блокирует ранний выход;
        b. signal strength delta блокирует выход на том же |score|;
        c. profit guard блокирует выход когда позиция в плюсе.
  4. SESSION_START_CAPITAL = 500 по умолчанию (без override через env).
  5. clear_backtest_signals дефолт = 500.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class TestReentryCooldown(unittest.TestCase):
    """Cooldown: после reversal-close следующий цикл не лезет обратно."""

    def setUp(self):
        # Изолированный импорт под patched env, чтобы не зацепить чужие cooldown.
        if "signal_trader" in sys.modules:
            del sys.modules["signal_trader"]

    def test_cooldown_arms_and_expires(self):
        with patch.dict(os.environ, {"AUTOTRADE_REENTRY_COOLDOWN_MIN": "30"}, clear=False):
            import config
            import importlib
            importlib.reload(config)
            if "signal_trader" in sys.modules:
                del sys.modules["signal_trader"]
            import signal_trader

            signal_trader._reentry_cooldowns.clear()
            self.assertFalse(signal_trader._reentry_blocked("BTC"))

            now = 1_000_000.0
            signal_trader._arm_reentry_cooldown("BTC", now_ts=now)
            # Через 1 минуту после reversal — всё ещё блокируем.
            self.assertTrue(signal_trader._reentry_blocked("BTC", now_ts=now + 60))
            # Через 29 минут — всё ещё блокируем.
            self.assertTrue(signal_trader._reentry_blocked("BTC", now_ts=now + 29 * 60))
            # Через 31 минуту — отпускаем.
            self.assertFalse(signal_trader._reentry_blocked("BTC", now_ts=now + 31 * 60))

    def test_cooldown_zero_minutes_means_disabled(self):
        with patch.dict(os.environ, {"AUTOTRADE_REENTRY_COOLDOWN_MIN": "0"}, clear=False):
            import config
            import importlib
            importlib.reload(config)
            if "signal_trader" in sys.modules:
                del sys.modules["signal_trader"]
            import signal_trader

            signal_trader._reentry_cooldowns.clear()
            signal_trader._arm_reentry_cooldown("BTC", now_ts=1_000_000.0)
            # Cooldown=0 → не блокируем никогда, даже мгновенно после arm.
            self.assertFalse(signal_trader._reentry_blocked("BTC", now_ts=1_000_000.5))


class TestPositionAge(unittest.TestCase):
    """_position_age_minutes должен корректно парсить created_at / entry_ts."""

    def setUp(self):
        if "signal_trader" in sys.modules:
            del sys.modules["signal_trader"]
        import signal_trader  # noqa: F401
        self.signal_trader = sys.modules["signal_trader"]

    def test_entry_ts_in_meta_wins(self):
        # entry_ts на 10 минут назад — должен вернуть ≈10.
        ten_min_ago = time.time() - 10 * 60
        meta = {"entry_ts": ten_min_ago}
        age = self.signal_trader._position_age_minutes({}, meta=meta)
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 10.0, delta=0.5)

    def test_falls_back_to_created_at(self):
        # SQLite-формат UTC, 5 минут назад.
        five_min_ago = datetime.now(timezone.utc).replace(microsecond=0) - \
            __import__("datetime").timedelta(minutes=5)
        position = {"created_at": five_min_ago.strftime("%Y-%m-%d %H:%M:%S")}
        age = self.signal_trader._position_age_minutes(position, meta={})
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 5.0, delta=0.5)

    def test_empty_returns_none(self):
        # Без entry_ts и без created_at — не можем определить → None.
        # Звено выше должно решить: разрешать ли reversal-close.
        self.assertIsNone(
            self.signal_trader._position_age_minutes({"created_at": ""}, meta={})
        )

    def test_garbage_created_at_returns_none(self):
        self.assertIsNone(
            self.signal_trader._position_age_minutes(
                {"created_at": "not a date"}, meta={}
            )
        )


class TestCloseOnSignalReversalGuards(unittest.IsolatedAsyncioTestCase):
    """Главный тест: ловим whiplash-сценарии один в один с продом."""

    async def asyncSetUp(self):
        if "signal_trader" in sys.modules:
            del sys.modules["signal_trader"]
        import signal_trader
        self.signal_trader = signal_trader
        signal_trader._reentry_cooldowns.clear()

    def _position(
        self,
        *,
        symbol: str = "BTC",
        direction: str = "BUY",
        entry: float = 79_130.0,
        age_minutes: float = 1.0,
        entry_signal_score: float = 16.0,
        entry_signal_direction: str = "SHORT",
    ) -> dict:
        meta = {
            "target": entry * 1.04,
            "stop": entry * 0.98,
            "entry_signal_score": entry_signal_score,
            "entry_signal_direction": entry_signal_direction,
            "entry_ts": time.time() - age_minutes * 60,
        }
        return {
            "id": -1,  # in-memory → не лезем в SQLite
            "symbol": symbol,
            "direction": direction,
            "entry_price": entry,
            "quantity": 0.01,
            "status": "open",
            "trade_log": json.dumps(meta),
            "created_at": "",
        }

    async def test_min_hold_blocks_immediate_reversal_close(self):
        """Production case: BUY @ $79,130, через минуту reversal SHORT (score=16.1) → не закрываем."""
        pos = self._position(age_minutes=1.0, entry_signal_score=16.0)
        prices = {"BTC": 79_064.60}  # PnL ≈ 0
        signal_bias = {"BTC": {"direction": "SHORT", "score": 16.1}}

        result = await self.signal_trader._close_on_signal_reversal(pos, prices, signal_bias)
        self.assertIsNone(result, "Свежая позиция не должна закрываться по reversal")

    async def test_strength_delta_blocks_same_score_reversal(self):
        """Score не изменился с момента входа → это та же noise-волна, не reversal."""
        pos = self._position(age_minutes=30.0, entry_signal_score=16.0)
        prices = {"BTC": 79_064.60}
        # Тот же |score|=16.1 что и при входе — никакого «усиления» нет.
        signal_bias = {"BTC": {"direction": "SHORT", "score": 16.1}}

        result = await self.signal_trader._close_on_signal_reversal(pos, prices, signal_bias)
        self.assertIsNone(result, "Reversal на том же |score| — не reversal")

    async def test_strong_strength_delta_allows_close(self):
        """Сигнал реально усилился (с 16 до 30) → честный разворот, закрываем."""
        pos = self._position(
            age_minutes=30.0,
            entry_signal_score=16.0,
            entry=79_130.0,
        )
        # Цена ушла против нас, PnL отрицательный — profit guard не сработает.
        prices = {"BTC": 78_000.0}
        signal_bias = {"BTC": {"direction": "SHORT", "score": 30.0}}

        # Подменяем БД-операции, чтобы не лезть в SQLite/GitHub.
        with patch.object(
            self.signal_trader, "update_backtest_capital", return_value=None
        ), patch.object(
            self.signal_trader, "get_backtest_config", return_value={"capital": 500.0}
        ), patch.object(
            self.signal_trader, "_export_backtest_snapshot", return_value=None
        ):
            result = await self.signal_trader._close_on_signal_reversal(pos, prices, signal_bias)

        self.assertIsNotNone(result, "Сильный reversal должен закрыть позицию")
        self.assertEqual(result["event"], "closed")
        self.assertIn("Signal reversal", result["reason"])
        # И после reversal-close активируется cooldown.
        self.assertTrue(self.signal_trader._reentry_blocked("BTC"))

    async def test_profit_guard_blocks_reversal_in_the_money(self):
        """Позиция уже +2% — отдаём trailing-стопу, не выходим по reversal."""
        pos = self._position(
            age_minutes=60.0,
            entry_signal_score=16.0,
            entry=79_000.0,
        )
        prices = {"BTC": 80_600.0}  # +2.0%
        signal_bias = {"BTC": {"direction": "SHORT", "score": 30.0}}

        result = await self.signal_trader._close_on_signal_reversal(pos, prices, signal_bias)
        self.assertIsNone(result, "В плюсе не закрываемся по reversal — пусть отрабатывает trailing")

    async def test_no_reversal_when_signal_aligned(self):
        """Sanity: signal_bias совпадает с direction → закрытия по reversal быть не должно."""
        pos = self._position(direction="BUY", age_minutes=60.0)
        prices = {"BTC": 79_500.0}
        signal_bias = {"BTC": {"direction": "LONG", "score": 50.0}}

        result = await self.signal_trader._close_on_signal_reversal(pos, prices, signal_bias)
        self.assertIsNone(result)


class TestSessionStartCapital(unittest.TestCase):
    """$500 по умолчанию — учебный счёт, $100 за 1.5 месяца съело в шум."""

    def test_default_is_500(self):
        # Сбрасываем env, чтобы не подцепить пользовательский override.
        env = {k: v for k, v in os.environ.items() if k != "AUTOTRADE_START_CAPITAL"}
        with patch.dict(os.environ, env, clear=True):
            for mod in ("session_manager",):
                if mod in sys.modules:
                    del sys.modules[mod]
            import session_manager
            self.assertEqual(session_manager.SESSION_START_CAPITAL, 500.0)

    def test_env_override_works(self):
        with patch.dict(os.environ, {"AUTOTRADE_START_CAPITAL": "1000"}, clear=False):
            for mod in ("session_manager",):
                if mod in sys.modules:
                    del sys.modules[mod]
            import session_manager
            self.assertEqual(session_manager.SESSION_START_CAPITAL, 1000.0)


class TestClearBacktestDefault(unittest.TestCase):
    """database.clear_backtest_signals дефолтный reset_capital = 500."""

    def test_default_kwarg(self):
        import inspect
        if "database" in sys.modules:
            del sys.modules["database"]
        import database
        sig = inspect.signature(database.clear_backtest_signals)
        self.assertEqual(sig.parameters["reset_capital"].default, 500.0)


if __name__ == "__main__":
    unittest.main()
