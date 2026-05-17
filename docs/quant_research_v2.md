# Quant-Filter v2 — Research Notes

> **Цель**: поднять hit-rate деривативного вердикта `/daily` с ~50% (coin flip)
> до **55–65%** на 1–5-дневном горизонте по криптовалютам, не вводя
> overclaim («AQR / Renaissance» — это маркетинг, реальный фундамент скромнее).

## TL;DR

| Стратегия              | overall hit | walk-forward robust | N    |
| ---------------------- | ----------- | ------------------- | ---- |
| baseline `MA50/200`    | **49.6%**   | 39.8                | 2088 |
| `v2_combo_final` (✓)   | **65.9%**   | **62.2**            |  276 |
| `v2_high_conv` (опц.)  | 75.0%       | 72.1                |  160 |
| `v2_donch_w_trend`     | 63.1%       | 59.9                |  260 |

- В прод идёт `v2_combo_final`: лучший trade-off между точностью и
  стабильностью. На 10 реальных вердиктах из `DIGEST_CACHE.md` показал
  **6/10 vs 5/10** у LLM-only — поймал SHORT @ 07.05 22:18, который бот пропустил.
- `v2_high_conv` оставлен в коде, но **не подключён к auto-filter** — это
  опциональный «триггер высокой уверенности», когда сходятся все три
  фильтра (RSI > 70 + MA50 < MA200 + BB extreme). Используется только если
  внешний слой явно его запросит.

## Что НЕ доказано (важно для честной оценки)

- Дата-сет: **только 1 год дневок** (May 2025 – May 2026), 5 крипто. На
  3-летнем 1h-окне результат может отличаться. Для перехода в продакшен
  с реальным капиталом — нужен повторный backtest на ccxt/kraken klines
  (Binance геоблокнут с части регионов).
- Нет LLM/news-компоненты — quant-filter работает только на цене. Это
  фича (детерминизм), но это также значит, что он слепой к шокам типа
  «ETF approval» или «exchange hack».
- Нет transaction costs / slippage / partial fills. На реальном live эти
  факторы съедают ~30–50% paper Sharpe.
- backtest ≠ live. Первые 30 дней boevike нужно вести параллельные логи
  (live entry vs paper entry vs quant-filter signal), и только после
  стабильного совпадения — повышать sizing.

## Структура решения

### 1. Pre-trade аналитика (in `quant_filter.py`)

Pure-module, без I/O. На вход — список закрытий, на выход — словарь:

```python
{
    "verdict": "LONG" | "SHORT" | "NEUTRAL",
    "confidence": 0..100,
    "reason": "…",
    "components": {bb_vote, donch_vote, rsi_vote, btc_trend},
    "features": {ma50, ma200, rsi14, bb_pos, …},
    "status": "ok" | "insufficient_history",
}
```

### 2. Логика голосования

```
BB-vote        : BB-position < 0.10  → LONG;  > 0.90  → SHORT;  else NEUTRAL
Donchian-vote  : пробил 20d-low      → LONG;  пробил 20d-high → SHORT
RSI-vote       : RSI(14) < 30        → LONG;  > 70         → SHORT
```

→ **2 из 3 совпали → ставим направление**, иначе NEUTRAL.

### 3. BTC regime gate

Считаем «собственное направление» актива (по 2-of-3 выше). Затем
смотрим тренд BTC: цена vs MA50 / MA200.

- BTC выше обеих MA → BTC trend = LONG
- BTC ниже обеих MA → BTC trend = SHORT
- Mixed → NEUTRAL (не блокирует)

Если собственный вердикт SHORT, а BTC strongly LONG → **демоутим до
NEUTRAL** (не ловим ножи против рынка). И наоборот.

### 4. Точки интеграции в основной код

| Файл                       | Функция                       | Что меняет                                                    |
| -------------------------- | ----------------------------- | ------------------------------------------------------------- |
| `web_search.py`            | `fetch_realtime_prices`       | Сохраняет `_closes_daily[-250]`, считает `quant_verdict` per asset |
| `web_search.py`            | `format_prices_for_agents`    | Рендерит строку «🟢 Quant: LONG (70%)» в `/markets` под SL/TP |
| `signals.py`               | `fetch_binance_signals`       | Тащит daily klines, считает quant verdict для autotrade       |
| `signals.py`               | `build_signal_bias_map`       | Бустит score на согласии, демоутит direction до NEUTRAL при сильном конфликте |
| `core/digest_context.py`   | `_aggregate_quant_verdicts`   | Сжимает per-symbol quant verdicts в overall LONG/SHORT/NEUTRAL |
| `core/digest_context.py`   | `build_digest_context`        | Принимает `quant_verdict_map=`, делает reconcile с LLM        |
| `main.py`                  | `_quant_map_from_prices`      | Helper: prices_dict → quant_verdict_map                       |

## Бэктест-методология

Скрипты в `/tmp/dxe_backtest/` (вне репо — это исследование, не код прода):

- `run.py` — baseline MA50/200 (49.6%, 830 verdicts)
- `sweep.py` — 20 стратегий (RSI, BB, Donchian, momentum, mean-rev, cross-asset)
- `walkforward.py` — rolling 90d/30d, 5 segments
- `v2_ensemble.py` — финальный `v2_combo_final` + `v2_high_conv`
- `check_real_verdicts.py` — sanity на 12 реальных вердиктов из DIGEST_CACHE.md

Данные: CoinGecko free API (без auth), 366 daily candles each, BTC/ETH/SOL/BNB/XRP.

## Failure modes

- **Insufficient history** (актив < 60 баров) → `verdict: NEUTRAL`, `status:
  insufficient_history`. Бот не падает, просто не показывает quant-строку.
- **API down** → graceful-degradation. `_fetch_daily_closes` возвращает
  пустой список, `quant_verdict` не вычисляется, signal_bias_map работает
  как до v2.
- **All three indicators conflict** (1 LONG + 1 SHORT + 1 NEUTRAL) →
  NEUTRAL. По дизайну: режим «cash by default», только сильное согласие
  выводит из кеша.

## Calibration parameters (если хочется тюнить)

```python
MA_FAST          = 50
MA_SLOW          = 200
RSI_PERIOD       = 14
BB_PERIOD        = 20
BB_STD_K         = 2.0
DONCHIAN_PERIOD  = 20
RSI_LOW          = 30.0    # ниже → меньше LONG-голосов
RSI_HIGH         = 70.0    # выше → меньше SHORT-голосов
BB_POS_LOW       = 0.10
BB_POS_HIGH      = 0.90
MIN_HISTORY_FOR_SIGNAL = 60
```

Менять — только если есть **out-of-sample** валидация. Не подгоняем
параметры под train-set, это смерть бэктеста.

## Что планируется дальше (out-of-scope этого PR)

1. ccxt/kraken backtest на 3 года 1h данных — подтверждение walk-forward
   robust на других режимах (2022-2024).
2. Подключение LLM-сигнала как 4-го голоса (LLM-vote weight = 1).
   Аккуратно: LLM голос только повышает confidence, не ставит direction
   в одиночку.
3. Sharpe-recipe: equity curve + max DD + win-rate + avg hold time
   (отдельный PR, не вклинивается в /daily).
