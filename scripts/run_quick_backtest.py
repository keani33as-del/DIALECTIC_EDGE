import asyncio
import json
import logging
import sys
from pathlib import Path

# Ensure repository root is in sys.path when running from scripts/
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtester import Backtester, get_candles
from trading_signal import parse_signals_from_backtest, timeframe_to_hours, Signal
from database import get_backtest_signals, init_db

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

async def main():
    # Ensure DB and tables exist
    await init_db()

    rows = await get_backtest_signals()
    print(f"Loaded {len(rows)} backtest rows from DB")

    signals = parse_signals_from_backtest(rows)
    print(f"Parsed {len(signals)} signals for backtesting")

    if not signals:
        print("No signals found in DB — generating sample signals for BTC/ETH")
        # Build sample signals based on latest candle close
        sample_assets = ["BTC", "ETH"]
        sample_signals = []
        for asset in sample_assets:
            candles = await get_candles(asset, timeframe_hours=24, limit=10)
            if not candles:
                continue
            last = candles[-1]
            entry = round(last.close, 2)
            target = round(entry * 1.04, 2)
            stop = round(entry * 0.98, 2)
            sig = Signal(
                asset=asset,
                direction="LONG",
                entry=entry,
                target=target,
                stop=stop,
                timeframe="1d",
                source="sample",
                timestamp=candles[0].timestamp.isoformat(),
                confidence=50.0,
                reason="sample"
            )
            if sig.validate():
                sample_signals.append(sig)
        if not sample_signals:
            print("Не удалось собрать sample signals — нет candle данных")
            return
        signals = sample_signals

    b = Backtester()
    candles_map = {}
    for s in signals:
        hours = timeframe_to_hours(s.timeframe)
        print(f"Fetching candles for {s.asset} timeframe {s.timeframe} ({hours}h)")
        candles = await get_candles(s.asset, timeframe_hours=hours, limit=200)
        candles_map[s.asset] = candles
        print(f"  -> fetched {len(candles)} candles for {s.asset}")

    results = b.test_signals(signals, candles_map)

    wins = len([r for r in results if r.result == 'WIN'])
    losses = len([r for r in results if r.result == 'LOSS'])
    timeouts = len([r for r in results if r.result == 'TIMEOUT'])
    no_entry = len([r for r in results if r.result == 'NO_ENTRY'])
    total_pnl = sum(r.pnl for r in results)

    summary = {
        'total': len(results),
        'wins': wins,
        'losses': losses,
        'timeouts': timeouts,
        'no_entry': no_entry,
        'total_pnl': total_pnl,
    }

    print('\nBACKTEST SUMMARY:')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print('\nDETAILS:')
    print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))

if __name__ == '__main__':
    asyncio.run(main())
