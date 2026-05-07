# Dialectic Edge — AI Trading System

> Multi-agent autonomous trading system with AI debate, regime-adaptive risk management, and real-time market signals.

*English version below Russian*

---

## 🇷🇺 РУССКАЯ ВЕРСИЯ

### Описание

**Dialectic Edge** — это полностью автономная торговая система, которая:

1. **Анализирует рынок** через мультиагентные AI-дебаты (Bull vs Bear vs Verifier vs Synth)
2. **Следит за сигналами** от профессиональных трейдеров (Bybit Markets Signals)
3. **Адаптируется к режиму рынка** (UPTREND / SIDEWAYS / HIGH_VOL / DOWNTREND)
4. **Управляет рисками** через Kelly Criterion, ATR, Trailing Stop, Split TP
5. **Торгует виртуально** на Railway (paper trading) с автосохранением состояния на GitHub

### Архитектура — как работает система

```
 ПОЛЬЗОВАТЕЛЬ (Telegram)
        │
        ▼
    ┌─────────┐
    │ main.py │  ← 3512 строк, точка входа
    │ Telegram│     все команды: /daily /analyze /starttrade /screener
    └────┬────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  analysis_service.py                              │
    │  • Собирает данные: news + prices + market_data │
    │  • Запускает AI-дебаты                           │
    │  • Формирует отчёт + торговый план               │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  agents.py — МУЛЬТИАГЕНТНЫЕ ДЕБАТЫ               │
    │                                                │
    │  🐂 Bull Researcher   — ищет бычьи аргументы   │
    │  🐻 Bear Skeptic       — ищет медвежьи аргументы│
    │  🔍 Data Verifier     — ловит галлюцинации     │
    │  ⚖️ Consensus Synth    — итоговый вердикт       │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  ai_provider.py — РОУТЕР МОДЕЛЕЙ                 │
    │                                                │
    │  Cerebras → Groq → Mistral → OpenRouter →       │
    │  Together → Gemini → Free models fallback        │
    │  (все бесплатные, 4+ провайдера)                │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  web_search.py — СБОРЩИК ДАННЫХ                 │
    │                                                │
    │  • Binance: BTC, ETH, SOL цены + объёмы        │
    │  • Yahoo Finance: SPX, NDX, VIX, DXY, GOLD    │
    │  • Alternative.me: Fear & Greed index          │
    │  • FRED (Federal Reserve): Fed Rate, CPI,       │
    │    Yield Curve, Fed Balance                     │
    │  • CoinGecko: BTC on-chain (MVRV, SOPR)        │
    │  • GDELT: геополитические события              │
    │  • Finnhub: market sentiment (опционально)       │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  market_indicators/ — СИСТЕМА БАЛЛОВ             │
    │                                                │
    │  • onchain.py: MVRV, SOPR, Exchange Reserves   │
    │  • macro_extended.py: QE/QT, Yield Curve,     │
    │    Credit Spreads                               │
    │  • scorer.py: Market Score (0-100),             │
    │    критические стоп-факторы                    │
    │  • aggregator.py: всё вместе → контекст для AI │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  signal_trader.py — АВТОТРЕЙДЕР (5 мин цикл)   │
    │                                                │
    │  ИСТОЧНИКИ СИГНАЛОВ:                           │
    │  1. Markets Signals (Bybit) — funding, OI, whales│
    │  2. Наши дайджесты (digest_score)              │
    │                                                │
    │  ФИЛЬТРЫ:                                      │
    │  • MVRV > 3.5 → не открываем LONG             │
    │  • MVRV < 1.0 → не открываем SHORT            │
    │  • Defense Mode → стоп всех позиций            │
    │  • Correlation Matrix → не дублируем риск    │
    │  • Adaptive thresholds (ChatGPT):              │
    │    HIGH_VOL → порог выше, позиция меньше     │
    │    SIDEWAYS → порог ещё выше                 │
    │    UPTREND → порог ниже (легче открыть)     │
    │                                                │
    │  ВЫХОД:                                       │
    │  • Split TP: 50% при +2% → full TP           │
    │  • Trailing Stop: активируется при +3%,      │
    │    движется за ценой (1.5%)                  │
    │  • Hard Stop: SL по ATR × regime              │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  TELEGRAM АЛЕРТЫ (автоматические)              │
    │                                                │
    │  • 🚨 MVRV > 4.0 — переоценён               │
    │  • 🔵 MVRV < 1.0 — историческое дно          │
    │  • 🛡️ Defense Mode активирован                 │
    │  • 📊 QE → QT смена                           │
    │  • 📈 Market Score ±8                          │
    │  • 🎯 Позиция открыта / закрыта              │
    └────────────────────────────────────────────────┘

### Конфигурация (Environment Variables)

Скопируй `.env.example` → `.env` и заполни ключи:

```env
# AI Модели (бесплатные)
GROQ_API_KEY=gsk_...
OPENROUTER_API_KEY=sk-or-v1-...
TOGETHER_API_KEY=tgp_v1_...
GEMINI_API_KEY=AIza...
CEREBRAS_API_KEY=csk-...

# Данные
FINNHUB_API_KEY=...
FRED_API_KEY=3b3477...
ALPHA_VANTAGE_API_KEY=...
TAVILY_API_KEY=tvly-...

# Telegram
BOT_TOKEN=123456:ABC-...

# GitHub (для state persistence)
GITHUB_TOKEN=ghp_...
GITHUB_REPO=ANAEHY/dialectic_edge
```

### Запуск

```bash
# Локально
python main.py

# Railway (автоматически)
# просто push → deploy
```

### Команды Telegram

| Команда | Описание |
|---------|----------|
| `/daily` | Полный AI-анализ рынка с дебатами |
| `/analyze <текст>` | Анализ конкретной новости |
| `/starttrade` | Запуск автотрейдера |
| `/stop` | Остановка автотрейдера |
| `/screener` | Сканер аномалий TOP-15 монет |
| `/why <SYMBOL>` | Почему открыта эта позиция |
| `/close <SYMBOL>` | Закрыть позицию вручную |
| `/health` | Health check (БД + GitHub + uptime) |
| `/stats` | Статистика бота |

---

## 📁 Структура файлов (подробно)

### Корневые файлы (точка входа)

| Файл | Строк | Назначение |
|------|-------|-----------|
| `main.py` | 3512 | Telegram bot, все handlers, диспетчер команд |
| `config.py` | — | Загрузка environment variables |
| `analysis_service.py` | 286 | Оркестратор: собирает данные → запускает дебаты → формирует отчёт |
| `ai_provider.py` | 765 | Роутер AI-моделей с fallback на 6 провайдеров |
| `agents.py` | 574 | 4 агента: Bull, Bear, Verifier, Synth + DebateOrchestrator |

### Торговля

| Файл | Назначение |
|------|------------|
| `signal_trader.py` | Автотрейдер: 5-мин цикл, открытие/закрытие позиций, trailing stop, split TP |
| `backtester.py` | Бэктест на исторических свечах (OHLC) |
| `signals.py` | Markets Signals API (Bybit): funding, OI, account ratio |
| `database.py` | SQLite: trade history, predictions, track record, backtest config |
| `session_manager.py` | Persistence виртуального капитала |
| `github_export.py` | Экспорт BACKTEST.md, FORECASTS.md, DIGEST_CACHE.md, MARKET_CACHE.md |

### AI и данные

| Файл | Назначение |
|------|------------|
| `web_search.py` | Realtime prices (Binance, Yahoo), Fear & Greed, макро с FRED |
| `data_sources.py` | Геополитика (GDELT), CPI YoY, экономический календарь |
| `cot_data.py` | Commitments of Traders (COT) от CFTC |
| `etf_flows.py` | ETF flows (SPY, QQQ, GLD) |
| `chart_generator.py` | Генерация изображений графиков |
| `pipeline.py` | Валидация сигналов |

### Модули `core/` (бизнес-логика)

| Файл | Назначение |
|------|------------|
| `regime_detector.py` | Определяет режим: UPTREND / SIDEWAYS / HIGH_VOL / DOWNTREND |
| `dynamic_risk.py` | Kelly Criterion + ATR-based position sizing |
| `confluence.py` | Confluence Score (0-100) из 10 источников |
| `whale_detector.py` | Whale order detection (Bybit large trades) |
| `correlation.py` | Correlation Matrix — блокирует дублирующие позиции |
| `event_defense.py` | Event Defense: стоп торговли перед high-impact новостями |
| `screener.py` | Market Screener:TOP-20 монет на аномалии |
| `multi_tf.py` | Multi-timeframe analysis (1h, 4h, 1d) |
| `data_enricher.py` | Обогащение candles техническими индикаторами |
| `economic_calendar.py` | Календарь макро-событий (FOMC, CPI, NFP) |
| `digest_context.py` | Контекст прошлых дайджестов для сравнения |

### Модули `market_indicators/` (новая система)

| Файл | Назначение |
|------|------------|
| `onchain.py` | BTC MVRV, SOPR, Exchange Reserves, Active Addresses (CoinGecko) |
| `macro_extended.py` | Fed Balance Sheet, QE/QT, Yield Curve, Credit Spreads (FRED) |
| `scorer.py` | Market Score: считает баллы по макро/ончейн/технике/сентименту |
| `aggregator.py` | Собирает всё + формирует контекст для AI агентов |
| `__init__.py` | Экспорты всех функций |

### Поддержка

| Файл | Назначение |
|------|------------|
| `scheduler.py` | Планировщик daily отчётов |
| `debate_storage.py` | Хранилище дебатов для листания |
| `learning.py` | Feedback система — улучшение моделей |
| `user_profile.py` | Профили пользователей |
| `alert_system.py` | Система алертов |
| `streamlit_app.py` | Web dashboard (PnL, trades, charts) |
| `weekly_report.py` | Еженедельный отчёт |
| `russia_agents.py` | Russia Edge — дополнительные агенты |

### Папки

| Папка | Назначение |
|-------|------------|
| `core/` | 13 модулей бизнес-логики |
| `market_indicators/` | 5 файлов on-chain + macro + scoring |
| `refactor/` | Рефакторинг: handlers/, interfaces/, prompts/, providers/ |
| `trading_system/` | CLI утилиты, batch runner, risk tools |
| `scripts/` | run_quick_backtest.py — быстрый бэктест |

---

## 🔑 Словарь терминов

### Режимы рынка

| Режим | Описание | Поведение автотрейдера |
|-------|---------|----------------------|
| **UPTREND** | Бычий тренд, MA50 > MA200 | Порог открытия ниже, позиции крупнее |
| **SIDEWAYS** | Консолидация, низкая волатильность | Порог выше, entry уже, позиции меньше |
| **HIGH_VOL** | Высокая волатильность (>5%) | Порог +4×conf + volatility penalty, entry шире, позиция -50% |
| **DOWNTREND** | Медвежий тренд | Порог выше, только SHORT или CASH |

### Индикаторы

| Индикатор | Источник | Описание |
|-----------|----------|----------|
| **MVRV** | CoinGecko (估算) | Market/Realized Value — переоценён > 3.5, дно < 1.0 |
| **SOPR** | CoinGecko | Spent Output Profit Ratio — фиксация > 1.05 |
| **Funding Rate** | Bybit | > 0.1% = быки платят медведям |
| **OI (Open Interest)** | Bybit | Рост OI + рост цены = подтверждение тренда |
| **VIX** | Yahoo (`^VIX`) | > 40 = кризис, < 15 = крайний оптимизм |
| **QE / QT** | FRED (WALCL) | QE = ликвидность растёт (бычий), QT = уходит (медвежий) |
| **Yield Curve** | FRED (DGS10-DGS2) | Инверсия < -0.5% = рецессия risk |
| **HY Spread** | FRED (HYCD) | > 5% = стресс на рынке |
| **Fear & Greed** | Alternative.me | < 25 = экстремальный страх (бычий), > 75 = жадность |
| **COT (Commitments of Traders)** | CFTC | Позиции крупных спекулянтов |

### Стратегия выхода

| Механизм | Описание |
|----------|----------|
| **Split TP** | 50% позиции закрывается при +2% profit |
| **Trailing Stop** | Активируется при +3% profit, движется за ценой (1.5% буфер) |
| **Hard Stop** | SL по ATR × regime multiplier |
| **Signal Reversal** | Закрытие при смене сигнала на противоположный |

### AI Агенты (дебаты)

| Агент | Промпт | Задача |
|-------|--------|--------|
| **Bull** | `BULL_SYSTEM` | Найти бычьи аргументы с цифрами из данных |
| **Bear** | `BEAR_SYSTEM` | Найти медвежьи аргументы с цифрами |
| **Verifier** | `VERIFIER_SYSTEM` | Удалить галлюцинации (статистика без источника) |
| **Synth** | `SYNTH_SYSTEM` | Итоговый вердикт + торговый план |

---

## 📊 Как работает каждый цикл автотрейдера

```
КАЖДЫЕ 5 МИНУТ:

1. build_consensus()
   → Markets Signals (funding, OI, whales) → candidates
   → Дайджесты (digest_score, regime, volatility) → candidates
   → MVRV + Fed Balance + Yield → candidates
   → Итог: список отранжированных кандидатов

2. _close_position_if_needed()
   → Проверяет: TP hit? SL hit? Trailing stop? Partial TP?
   → Сохраняет в БД, пишет BACKTEST.md, шлёт алерт в Telegram

3. rank_trade_candidates()
   → _score_candidate() с адаптивными порогами
   → HIGH_VOL → threshold +4×conf + volatility penalty
   → SIDEWAYS → threshold +3×conf
   → UPTREND → threshold -2×conf

4. MVRV hard-stop
   → MVRV > 3.5 + LONG → пропускаем
   → MVRV < 1.0 + SHORT → пропускаем

5. Open positions (до 5)
   → quantity_pct safety check (< 1e-4 → skip)
   → Volatility > 5% → size × 0.5
   → R/R < 1.5 → skip
   → Correlation conflict → skip
   → Defense mode → skip

6. save_market_cache()
   → MARKET_CACHE.md на GitHub (20 мин TTL)
```

---

## 🌍 Что такое ETF, COT, и почему они важны

### ETF (Exchange-Traded Funds)

ETF — это фонд который торгуется как акция. Важные ETF для крипто-трейдинга:

| ETF | Тикер | Что показывает |
|-----|-------|---------------|
| S&P 500 | SPY / ^GSPC | Здоровье экономики США |
| Nasdaq 100 | QQQ / ^NDX | Технологический сектор |
| Золото | GLD | Safe haven / инфляция |
| Нефть | USO / CL=F | Сырьё / геополитика |

**Как использовать:** когда SPY падает — крипта тоже падает (корреляция). Когда золото растёт — может означать risk-off. ETF данные берём из Yahoo Finance.

### COT (Commitments of Traders)

COT — еженедельный отчёт от CFTC (Commodity Futures Trading Commission). Показывает позиции трейдеров на фьючерсных рынках.

| Группа | Кто | Что показывает |
|--------|-----|---------------|
| **Commercial** | Хеджеры (банки, компании) | Страхуют свой бизнес |
| **Non-Commercial** | Крупные спекулянты | Большие деньги ставят |
| **Non-Reportable** | Мелкие | Обычно против тренда |

**Как использовать в проекте:** `cot_data.py` загружает COT для Bitcoin, Gold, Crude Oil. Если крупные спекулянты в NET SHORT — это медвежий сигнал.

### Funding Rate (Bybit)

Funding rate — это плата которую трейдеры платят друг другу каждые 8 часов. Если ставка **положительная** — длинные платят коротким → быки агрессивны. Если **отрицательная** — наоборот.

| Funding | Значение | Сигнал |
|---------|----------|--------|
| > +0.1% | Быки доминируют | Вероятность сброса |
| < -0.1% | Медведи доминируют | Сжимание шортов |

### OI (Open Interest)

Open Interest — общее количество открытых фьючерсных контрактов. Рост OI + рост цены = подтверждение. Рост OI + падение цены = дивергенция.

---

## 🔗 Взаимосвязь файлов (dependency graph)

```
main.py
  ├── analysis_service.py
  │     ├── agents.py (DebateOrchestrator)
  │     │     └── ai_provider.py
  │     ├── web_search.py (get_full_realtime_context)
  │     ├── market_indicators/ (build_enriched_context)
  │     │     ├── onchain.py (fetch_btc_onchain)
  │     │     ├── macro_extended.py (fetch_extended_macro)
  │     │     └── scorer.py (calculate_market_score)
  │     └── data_sources.py
  │
  ├── signal_trader.py
  │     ├── signals.py (build_signal_bias_map)
  │     ├── core/regime_detector.py
  │     ├── core/whale_detector.py
  │     ├── core/correlation.py
  │     ├── core/event_defense.py
  │     └── github_export.py (MARKET_CACHE.md)
  │
  ├── database.py
  │     ├── init_db()
  │     ├── get_backtest_signals()
  │     └── append_trade_decision_log()
  │
  └── scheduler.py
        └── github_export.py (DIGEST_CACHE.md, FORECASTS.md)
```

---

## ⚙️ Адаптивные параметры автотрейдера

| Параметр | Базовое | UPTREND | SIDEWAYS | HIGH_VOL |
|----------|---------|---------|----------|----------|
| `OPEN_SCORE_THRESHOLD` | 12.0 | 10.4 | 16.5 | 18+ |
| `ENTRY_TOLERANCE_PCT` | 2% | 2% | 1.2% | 3% |
| Позиция при vol>5% | 100% | 100% | 100% | 50% |

---

## 📦 GitHub файлы (автообновляемые)

| Файл | Обновляется | Содержит |
|------|-------------|----------|
| `BACKTEST.md` | Каждая закрытая сделка | История P&L |
| `FORECASTS.md` | После /daily | Track record всех прогнозов |
| `DIGEST_CACHE.md` | После /daily | Последние 14 дайджестов |
| `MARKET_CACHE.md` | Каждый цикл | MVRV, QE/QT, VIX, Score |

---

## 🇬🇧 ENGLISH VERSION

### Dialectic Edge — AI Trading System

**What it does:**
1. **Analyzes market** via multi-agent AI debates
2. **Monitors** Bybit Markets Signals (funding, OI, whales)
3. **Adapts** to market regime (UPTREND / SIDEWAYS / HIGH_VOL)
4. **Manages risk** via Kelly Criterion, ATR, Trailing Stop, Split TP
5. **Paper trades** on Railway with GitHub state persistence

### Architecture

```
User → main.py (Telegram)
         ↓
  analysis_service.py
         ↓
  agents.py (Bull/Bear/Verifier/Synth)
         ↓
  ai_provider.py (Cerebras/Groq/Mistral/OpenRouter/Together/Gemini)
         ↓
  web_search.py (Binance/Yahoo/FRED/CoinGecko/GDELT)
         ↓
  market_indicators/ (on-chain + macro + scoring)
         ↓
  signal_trader.py (auto-trader, 5-min cycle)
         ↓
  GitHub (BACKTEST.md / FORECASTS.md / MARKET_CACHE.md)
```

### Key Features

- **10 elite core modules** (regime, risk, confluence, whale, correlation, etc.)
- **100% free AI models** (Cerebras, Groq, Mistral, OpenRouter, Together, Gemini)
- **Adaptive thresholds** based on market regime + volatility
- **Trailing stop** + **Split TP** for optimal exits
- **MVRV hard-stops** (no LONG when >3.5, no SHORT when <1.0)
- **Event Defense** blocks trades before high-impact macro events
- **GitHub persistence** survives Railway redeployments
- **Telegram alerts** on MVRV, Defense Mode, QE/QT changes

### Setup

```bash
cp .env.example .env
# fill in API keys
python main.py
```

### Commands

| Command | Description |
|---------|-------------|
| `/daily` | Full AI market analysis |
| `/starttrade` | Start autotrader |
| `/screener` | Anomaly scanner TOP-15 |
| `/health` | Health check |

---

## 📜 Disclaimer

⚠️ **This is a paper trading / educational project.**  
All trades are simulated. Nothing here is financial advice.  
Past performance does not guarantee future results.  
DYOR (Do Your Own Research).