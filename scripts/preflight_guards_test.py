#!/usr/bin/env python3
"""Pre-flight integration test of new signals guards (PR #30).

Exercises _render_trade_plan_from_json with:
  - Real-time BTC/ETH/SOL/BNB/XRP prices from CoinGecko
  - 6 realistic synthetic Synth JSON outputs covering all guard paths

Run:  python3 scripts/preflight_guards_test.py

No LLM keys needed. Output: human-readable report of how every guard
behaved on every scenario. Use this to verify guards are not over-firing
before tomorrow's live trading.
"""
from __future__ import annotations

import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Workaround core/__init__.py circular import: load core.horizons directly.
import importlib.util  # noqa: E402
import types  # noqa: E402

core_mod = types.ModuleType("core")
core_mod.__path__ = [str(ROOT / "core")]
sys.modules["core"] = core_mod

spec = importlib.util.spec_from_file_location("core.horizons", str(ROOT / "core" / "horizons.py"))
horizons = importlib.util.module_from_spec(spec)
sys.modules["core.horizons"] = horizons
spec.loader.exec_module(horizons)
core_mod.horizons = horizons

spec2 = importlib.util.spec_from_file_location("agents", str(ROOT / "agents.py"))
agents = importlib.util.module_from_spec(spec2)
sys.modules["agents"] = agents
spec2.loader.exec_module(agents)


# Capture WARNING-level logs so we know exactly which guards fired.
class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(f"[{record.levelname}] {record.getMessage()}")

cap = _Capture()
logging.getLogger().addHandler(cap)
logging.getLogger().setLevel(logging.WARNING)
# Stop "agents" propagation→root double-logging; route only through one handler.
logging.getLogger("agents").propagate = True
logging.getLogger("agents").handlers.clear()


def fetch_realtime_prices() -> dict[str, float]:
    """Returns {SYMBOL: USD price} from CoinGecko (public, no auth)."""
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=bitcoin,ethereum,solana,binancecoin,ripple&vs_currencies=usd"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read())
    except urllib.error.URLError as e:
        print(f"[WARN] CoinGecko fetch failed: {e}", file=sys.stderr)
        return {}
    return {
        "BTC": float(data["bitcoin"]["usd"]),
        "ETH": float(data["ethereum"]["usd"]),
        "SOL": float(data["solana"]["usd"]),
        "BNB": float(data["binancecoin"]["usd"]),
        "XRP": float(data["ripple"]["usd"]),
    }


def build_scenarios(prices: dict[str, float]) -> list[dict]:
    """6 realistic Synth outputs covering all guard paths.

    Use current real prices to construct realistic plans.
    """
    btc = prices.get("BTC", 80000.0)
    eth = prices.get("ETH", 2300.0)
    sol = prices.get("SOL", 95.0)

    return [
        {
            "_name": "1. BULL+LONG normal (no guards fire) — baseline",
            "_expected": "Plan rendered as LONG with reasonable SL/TP",
            "synth": {
                "verdict": "БЫЧИЙ",
                "reason": "тест: MA200 пробит вверх, smart-money L/S 1.4, Coinbase premium +0.15%",
                "plans": [
                    {
                        "symbol": "BTC",
                        "direction": "LONG",
                        "entry": btc * 1.005,
                        "stop": btc * 0.98,
                        "target": btc * 1.06,
                        "rr": "1:2.2",
                        "size": "10%",
                        "horizon": "7-14д",
                        "trigger": "пробой выше",
                    }
                ],
                "watch": [],
                "key_trigger": "пробой выше",
                "invalidation": "закрытие ниже ATH-5%",
                "simple": "тест",
                "eli5": "тест",
                "qe_qt": "NEUTRAL",
            },
        },
        {
            "_name": "2. BEAR+SHORT stale entry (April-style bug)",
            "_expected": "Stale-price guard → CASH + ⚠️ DATA STALE",
            "synth": {
                "verdict": "МЕДВЕЖИЙ",
                "reason": "тест: MVRV 3.6, COT SHORT, OIL +12% MoM",
                "plans": [
                    {
                        "symbol": "BTC",
                        "direction": "SHORT",
                        "entry": btc * 1.10,
                        "stop": btc * 1.13,
                        "target": btc * 1.00,
                        "rr": "1:3",
                        "size": "5%",
                        "horizon": "7-14д",
                        "trigger": "rejection",
                    }
                ],
                "watch": [],
                "key_trigger": "rejection",
                "invalidation": "пробой выше",
                "simple": "тест",
                "eli5": "тест",
                "qe_qt": "NEUTRAL",
            },
        },
        {
            "_name": "3. BEAR + LONG inconsistency (11.04/22.04 case, fresh prices)",
            "_expected": "Consistency guard → ⚠️ ВНИМАНИЕ плашка",
            "synth": {
                "verdict": "МЕДВЕЖИЙ",
                "reason": "тест: верификатор недоволен",
                "plans": [
                    {
                        "symbol": "BTC",
                        "direction": "LONG",
                        "entry": btc * 1.005,
                        "stop": btc * 0.965,
                        "target": btc * 1.085,
                        "rr": "1:2",
                        "size": "10%",
                        "horizon": "7-14д",
                        "trigger": "breakout",
                    }
                ],
                "watch": [],
                "key_trigger": "breakout",
                "invalidation": "ниже MA50",
                "simple": "тест",
                "eli5": "тест",
                "qe_qt": "NEUTRAL",
            },
        },
        {
            "_name": "4. BULL+LONG with too-tight SL (April BTC $77,600 SL $75k case)",
            "_expected": "SL-guard → SL расширен до 1.5%, либо R/R упал → CASH",
            "synth": {
                "verdict": "БЫЧИЙ",
                "reason": "тест: тесный стоп",
                "plans": [
                    {
                        "symbol": "BTC",
                        "direction": "LONG",
                        "entry": btc * 1.005,
                        "stop": btc * 1.000,
                        "target": btc * 1.04,
                        "rr": "1:7",  # SL-distance guard расширит SL до 1.5%, R/R пересчитается
                        "size": "10%",
                        "horizon": "7-14д",
                        "trigger": "breakout",
                    }
                ],
                "watch": [],
                "key_trigger": "breakout",
                "invalidation": "ниже MA50",
                "simple": "тест",
                "eli5": "тест",
                "qe_qt": "NEUTRAL",
            },
        },
        {
            "_name": "5. NEUTRAL + ETH watch list (типичный осторожный день)",
            "_expected": "Без плашек, корректный watch-режим",
            "synth": {
                "verdict": "НЕЙТРАЛЬНЫЙ",
                "reason": "тест: смешанные сигналы, ATR низкий",
                "plans": [
                    {
                        "symbol": "ETH",
                        "direction": "CASH",
                        "trigger": f"пробой выше ${eth*1.04:.0f} вверх ИЛИ ниже ${eth*0.96:.0f}",
                        "horizon": "7-14д",
                    }
                ],
                "watch": [
                    {"symbol": "ETH", "level": f"${eth:.0f}", "note": "ждём пробоя"},
                    {"symbol": "SOL", "level": f"${sol:.0f}", "note": "следим за объёмом"},
                ],
                "key_trigger": "пробой границ диапазона",
                "invalidation": "закрытие внутри",
                "simple": "тест",
                "eli5": "тест",
                "qe_qt": "NEUTRAL",
            },
        },
        {
            "_name": "6. BEAR with stop_factor=bearish (LONG plan → блокировка)",
            "_expected": "Stop-factor block → CASH (старый guard, не сломан)",
            "synth": {
                "verdict": "МЕДВЕЖИЙ",
                "reason": "тест: MVRV 4.0 + VIX > 35",
                "plans": [
                    {
                        "symbol": "BTC",
                        "direction": "LONG",
                        "entry": btc * 1.005,
                        "stop": btc * 0.98,
                        "target": btc * 1.06,
                        "rr": "1:2.2",
                        "size": "10%",
                        "horizon": "7-14д",
                        "trigger": "breakout",
                    }
                ],
                "watch": [],
                "key_trigger": "breakout",
                "invalidation": "закрытие ниже",
                "simple": "тест",
                "eli5": "тест",
                "qe_qt": "NEUTRAL",
            },
            "_stop_factor": "bearish",
        },
        {
            "_name": "7. Per-asset coverage (5 crypto + 6 macro, double-trigger MA CASH plans)",
            "_expected": "Все 11 планов рендерятся как CASH (double-trigger MA не demote'ится в watch)",
            "synth": {
                "verdict": "НЕЙТРАЛЬНЫЙ",
                "reason": "тест: per-asset coverage — каждый актив со своими MA200/MA50 триггерами",
                "plans": [
                    {"symbol": "BTC", "direction": "CASH", "horizon": "7-14д",
                     "trigger": f"закрытие выше ${btc * 1.02:.0f} (MA200) → откроем LONG; пробой ${btc * 0.92:.0f} (MA50) вниз → откроем SHORT"},
                    {"symbol": "ETH", "direction": "CASH", "horizon": "7-14д",
                     "trigger": f"закрытие выше ${eth * 1.04:.0f} (MA200) → откроем LONG; пробой ${eth * 0.95:.0f} (MA50) вниз → откроем SHORT"},
                    {"symbol": "SOL", "direction": "CASH", "horizon": "7-14д",
                     "trigger": f"закрытие выше ${sol * 1.17:.0f} (MA200) → откроем LONG; пробой ${sol * 0.90:.0f} (MA50) вниз → откроем SHORT"},
                    {"symbol": "BNB", "direction": "CASH", "horizon": "7-14д",
                     "trigger": "закрытие выше $720 (MA200) → откроем LONG; пробой $640 (MA50) вниз → откроем SHORT"},
                    {"symbol": "XRP", "direction": "CASH", "horizon": "7-14д",
                     "trigger": "закрытие выше $1.65 (MA200) → откроем LONG; пробой $1.30 (MA50) вниз → откроем SHORT"},
                    {"symbol": "SPX", "direction": "CASH", "horizon": "7-14д",
                     "trigger": "закрытие выше 6764 (MA200) → откроем LONG; пробой 6884 (MA50) вниз → откроем SHORT"},
                    {"symbol": "NDX", "direction": "CASH", "horizon": "7-14д",
                     "trigger": "закрытие выше 25016 (MA200) → откроем LONG; пробой 25688 (MA50) вниз → откроем SHORT"},
                    {"symbol": "GOLD", "direction": "CASH", "horizon": "7-14д",
                     "trigger": "закрытие выше $4750 (MA50) → откроем LONG; пробой $4306 (MA200) вниз → откроем SHORT"},
                    {"symbol": "OIL_WTI", "direction": "CASH", "horizon": "7-14д",
                     "trigger": "закрытие выше $96 (MA50) → откроем LONG; пробой $70 (MA200) вниз → откроем SHORT"},
                    {"symbol": "DXY", "direction": "CASH", "horizon": "7-14д",
                     "trigger": "закрытие выше 98.97 (MA50) → откроем LONG; пробой 98.55 (MA200) вниз → откроем SHORT"},
                    {"symbol": "VIX", "direction": "CASH", "horizon": "7-14д",
                     "trigger": "закрытие выше 21.86 (MA50) → откроем LONG (хедж); пробой 18.35 (MA200) вниз → SHORT"},
                ],
                "watch": [],
                "key_trigger": "пробой $82307 → бычий разворот по BTC",
                "invalidation": "BTC закрытие ниже $74600",
                "simple": "тест",
                "eli5": "тест",
                "qe_qt": "NEUTRAL",
            },
        },
    ]


def main() -> int:
    print("=" * 78)
    print("PRE-FLIGHT GUARDS TEST — PR #30 (anti-bearish + stale/SL/consistency)")
    print("=" * 78)

    prices = fetch_realtime_prices()
    if not prices:
        print("[FATAL] No prices fetched. CoinGecko may be down. Bailing.", file=sys.stderr)
        return 1

    print("\n📊 РЕАЛЬНЫЕ ЦЕНЫ С CoinGecko (для контекста):")
    for sym, px in sorted(prices.items()):
        print(f"  {sym}: ${px:,.2f}")

    pack = horizons.get_horizon("swing")
    scenarios = build_scenarios(prices)

    all_passed = True
    for i, scn in enumerate(scenarios, 1):
        cap.lines.clear()
        name = scn["_name"]
        expected = scn["_expected"]
        synth = scn["synth"]
        stop_factor = scn.get("_stop_factor")

        print("\n" + "─" * 78)
        print(f"СЦЕНАРИЙ {i}: {name}")
        print(f"Ожидание: {expected}")
        if stop_factor:
            print(f"stop_factor: {stop_factor}")
        print("─" * 78)

        try:
            rendered = agents._render_trade_plan_from_json(
                synth, horizon_pack=pack, stop_factor=stop_factor, market_prices=prices
            )
        except Exception as e:
            print(f"❌ EXCEPTION: {e}")
            all_passed = False
            continue

        # Print only the first 25 lines of digest to keep report compact.
        digest_lines = rendered.splitlines()
        print("\n📋 РЕНДЕР ДАЙДЖЕСТА:")
        for ln in digest_lines[:25]:
            print(f"  {ln}")
        if len(digest_lines) > 25:
            print(f"  ... ({len(digest_lines) - 25} more lines)")

        if cap.lines:
            print("\n🔔 GUARD WARNINGS:")
            for w in cap.lines:
                print(f"  {w}")
        else:
            print("\n🔔 GUARD WARNINGS: (none — clean baseline)")

    print("\n" + "=" * 78)
    print("РЕЗЮМЕ:")
    print("  Все сценарии отрендерены без исключений." if all_passed else "  Есть исключения — смотри выше.")
    print("  Сравни 'Ожидание' с реальным выводом по каждому сценарию.")
    print("=" * 78)
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
