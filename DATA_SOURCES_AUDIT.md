# Data Sources Audit — что мы можем добавить, чему доверять

> Аудит open-source библиотек и API, которые могут дать движку Dialectic Edge
> новые сигналы. Каждый источник оценён по: **надёжность** (кто публикует),
> **free tier** (можно ли пользоваться без оплаты), **уровень шума** (что
> пройдёт через фильтр calibration), **сложность интеграции** (часы работы).
>
> Цель — не «добавить ещё 20 источников», а понять что реально стоит ставить
> в backlog. Большинство шумного крипто-Twitter / sentiment-aggregator'ов
> отброшены — они дают correlation с ценой, не predictive edge.

---

## TL;DR

**Готовы к интеграции (есть free API + проверенные данные):**
- ✅ **FRED** (Federal Reserve Economic Data) — макро, монетарка, infl.
- ✅ **CFTC COT** (уже используется частично) — позиции крупных спекулянтов.
- ✅ **DefiLlama** (TVL, stablecoin supply, DEX volume).
- ✅ **Deribit API** (опционы по BTC/ETH — IV, skew, GEX).
- ✅ **Glassnode Studio** (бесплатный slice MVRV / NUPL / Reserve Risk).
- ✅ **Coinglass** (open interest + funding + liquidations агрегированные).
- ✅ **Binance Futures** (funding history + Long/Short Account Ratio).
- ✅ **Alternative.me** (Fear & Greed Index — уже частично используется).
- ✅ **ETF Flow** (BitMEX Research / SoSoValue) — спот-ETF потоки BTC/ETH.

**Стоит пилотировать (есть данные, но шумные / hard to calibrate):**
- 🟡 **Santiment** (free tier ограниченный, social-volume может быть useful).
- 🟡 **Kaiko / CryptoCompare** (orderbook data, обычно платный).
- 🟡 **TheTie** (sentiment, но коммерческий).
- 🟡 **CoinMetrics community** (NVT, MVRV-Z — slice бесплатный).

**Не рекомендую (либо unreliable, либо noise > signal):**
- ❌ **Trade-with-AI / GPT-trader репо** — нет track record.
- ❌ **CryptoPanic** sentiment — sentiment-aggregator без provenance.
- ❌ **Lunarcrush** — social score, на бэктестах не воспроизводится.
- ❌ **Whale Alert** — large transactions, но без context это шум.

---

## Раздел 1. Макро / монетарная политика

### 1.1. FRED (Federal Reserve Economic Data)

- **URL:** https://fred.stlouisfed.org/docs/api/fred/
- **Python:** `fredapi`, `pandas-datareader`
- **Free tier:** да (50,000 req/day по API key — бесплатный)
- **Что даёт:**
  - DGS10 (10-year Treasury yield) — risk-on/risk-off proxy
  - VIXCLS (VIX closing) — fear gauge
  - DXY (USD index) — обратная корреляция с BTC
  - M2SL (M2 money supply) — длинный цикл крипты
  - FEDFUNDS (Fed Funds rate)
  - DGS2 (2-year yield), spread DGS10-DGS2 (рецессия)
  - DTWEXBGS (broad USD trade-weighted)
  - UMCSENT (Michigan consumer sentiment)
  - CPIAUCSL, CPILFESL (CPI и core CPI)
- **Качество данных:** ★★★★★ (источник — ФРС, обновление daily/monthly).
- **Шум:** низкий (макро ряды — не tick-level спекулянтов).
- **Сложность интеграции:** ★☆☆☆☆ (~2 часа: get API key, добавить в config,
  написать `core/macro_fetcher.py` с кэшем 1 час).
- **Что делать в проекте:**
  - Добавить regime-feature: `is_falling_dollar` (DXY 5d trend),
    `is_rate_cut_cycle` (FFR drop).
  - Conditional model: bull setup × is_falling_dollar = +bonus.
  - Predictor для weekly walk-forward.

### 1.2. CFTC COT Reports (уже используется частично)

- **URL:** https://www.cftc.gov/MarketReports/CommitmentsofTraders/
- **Python:** `cot_reports`, `cftc-api` (есть пара рабочих оберток)
- **Free tier:** да, отчёты публичные.
- **Что даёт:** позиции крупных спекулянтов и хеджеров по BTC futures CME,
  E-mini S&P, gold, EUR, ...
- **Качество:** ★★★★★ (regulator data, weekly Friday).
- **Шум:** низкий, но **lag = неделя** — это не intraday-signal.
- **Что у нас сейчас:** Bear Skeptic тащит «Large Specs NET SHORT = -5,551»
  как раз отсюда. Но мы НЕ калибровали этот сигнал отдельно — это
  contrarian (исторически) или sell-signal (в моменте)? **PR #2** (per-signal
  calibration) ответит на этот вопрос.

### 1.3. ECB / Bank of England / BIS

- **URL:** https://sdw.ecb.europa.eu/, https://www.bankofengland.co.uk/statistics
- **Free tier:** да, открытые API.
- **Что даёт:** EU/UK макроряды (Euribor, Bank Rate).
- **Шум:** низкий.
- **Сложность:** ★★★☆☆ (другие схемы API, форматы).
- **Приоритет:** низкий — FRED покрывает 95% корреляций с крипто.

---

## Раздел 2. On-chain / blockchain метрики

### 2.1. Glassnode Studio (бесплатный slice)

- **URL:** https://studio.glassnode.com/
- **Python:** `glassnode-client` (community) или прямой REST
- **Free tier:** да, ~25-30 метрик бесплатно с lag 24h
- **Что даёт (бесплатно):**
  - SOPR (Spent Output Profit Ratio) — реализация прибыли/убытка
  - aSOPR (adjusted SOPR) — без учёта < 1h транзакций
  - Net Unrealized P/L (NUPL)
  - Active Addresses
  - Transaction Count
- **Что даёт (paid):**
  - MVRV-Z Score (один из лучших top/bottom indicators)
  - Reserve Risk
  - Coin Days Destroyed (CDD)
  - Exchange Net Flow (BTC/ETH on/off exchanges)
- **Качество:** ★★★★☆ (top-tier on-chain analytics, бесплатный slice limited).
- **Шум:** низкий для bottom-indicators, средний для intraday.
- **Сложность:** ★★☆☆☆ (REST API, ~3 часа на 5 метрик).
- **Альтернативы (если paid не вариант):**
  - **CoinMetrics community** — free slice MVRV / NVT.
  - **CryptoQuant free** — Exchange Reserves, Exchange Flows.

### 2.2. DefiLlama

- **URL:** https://api.llama.fi/, https://defillama.com/docs/api
- **Python:** `defillama2`, `requests` напрямую
- **Free tier:** да, **полностью бесплатный без API key**.
- **Что даёт:**
  - TVL (Total Value Locked) по всем chain'ам и протоколам
  - Stablecoin supply: USDT/USDC mcap по chain'ам (важно: rising stablecoin
    supply = риск-on, falling = риск-off)
  - DEX volume per chain
  - Bridge flows
  - Yield rates (APY) по протоколам
  - Fees: revenue & fees per protocol
- **Качество:** ★★★★★ (комьюнити open-source, проверяется множеством).
- **Шум:** низкий.
- **Сложность:** ★☆☆☆☆ (~1 час на интеграцию).
- **Применение:**
  - Feature: `stablecoin_mcap_7d_change` — это leading indicator для BTC.
  - Feature: `is_stables_inflowing` — для regime detector.

### 2.3. CoinMetrics community

- **URL:** https://coinmetrics.io/community-data-dictionary/
- **Free tier:** да, CSV/Parquet snapshot обновляются ежедневно
- **Что даёт:**
  - NVT (Network Value to Transactions) — классический valuation
  - MVRV (community version)
  - Realized Cap
  - PriceUSD, ROI, supply metrics
- **Сложность:** ★★☆☆☆ (нужно качать CSV, парсить).
- **Применение:** weekly walk-forward feature.

### 2.4. CryptoQuant free tier

- **URL:** https://cryptoquant.com/
- **Free tier:** да, ~10 метрик с lag 30min-1h
- **Что даёт:**
  - Exchange Inflow/Outflow (хороший proxy для capitulation)
  - Miner Outflow (хеджирование майнеров)
  - Stablecoin Supply Ratio
- **Качество:** ★★★★☆.
- **Сложность:** ★★★☆☆ (нужна регистрация, scraping вместо API на free).

---

## Раздел 3. Деривативы / опционы

### 3.1. Deribit API

- **URL:** https://docs.deribit.com/
- **Python:** `deribit-api-python`, или REST/WebSocket напрямую
- **Free tier:** **полностью бесплатный**, не нужен account для read-only.
- **Что даёт:**
  - Implied volatility (IV) по всем strikes
  - 25-delta skew (puts vs calls 25-delta IV difference) —
    один из лучших fear indicators
  - DVOL index (BTC IV index, аналог VIX для BTC)
  - Put/Call ratio (open interest и volume)
  - Max Pain (strike с максимальным OI)
  - Term structure (1w / 1m / 3m / 6m IV)
  - Realized vs Implied volatility spread
- **Качество:** ★★★★★ (90%+ BTC/ETH options volume идёт через Deribit).
- **Шум:** низкий для daily features, средний для intraday.
- **Сложность:** ★★★☆☆ (~6-8 часов: тянуть chain, считать skew и GEX).
- **Применение:**
  - Feature: `dvol_5d_zscore` — экстремальный fear/greed regime.
  - Feature: `put_call_ratio` — sentiment proxy.
  - Feature: `25d_skew` — direction of fear (left tail vs right tail).
- **Стоит делать.** Это даёт нам **forward-looking** info, чего сейчас нет.

### 3.2. Coinglass

- **URL:** https://www.coinglass.com/, https://open-api.coinglass.com/
- **Free tier:** да (бесплатный API key, ~30 req/min)
- **Что даёт:**
  - Aggregated open interest по всем биржам
  - Long/Short ratio (Binance, Bybit, OKX)
  - Liquidations (24h) по символам
  - Funding rate history
  - Top trader positions (Binance leaderboard summary)
- **Качество:** ★★★★☆.
- **Шум:** средний (особенно liquidations — после факта).
- **Сложность:** ★★☆☆☆ (~3 часа).
- **Применение:**
  - Feature: `liquidation_clusters` — leverage flush proxy.
  - Feature: `oi_24h_change` — позиционирование.

### 3.3. Binance Futures public endpoints

- **URL:** https://binance-docs.github.io/apidocs/futures/en/
- **Free tier:** да, public endpoints без API key.
- **Что даёт:**
  - Funding rate history
  - Long/Short Account Ratio (по адресам)
  - Top Trader Long/Short Ratio (по top 20%)
  - Open Interest history
- **Качество:** ★★★★★.
- **Сложность:** ★★☆☆☆ (~2-3 часа).
- **Текущий статус:** у нас уже частично интегрировано (`signals.py`).

---

## Раздел 4. Спот / биржи / orderbook

### 4.1. Kaiko (paid)

- **URL:** https://www.kaiko.com/
- **Free tier:** trial only, после нужен enterprise.
- **Что даёт:** L2 orderbook snapshot data, trade tape, cross-venue.
- **Качество:** ★★★★★ (institutional grade).
- **Применение:** нужно для VPIN / Kyle's lambda. Без этого
  microstructure features недоступны.
- **Решение:** **скип на этапе MVP**, рассмотреть после $10K MRR.

### 4.2. CCXT (универсальная обёртка для бирж)

- **URL:** https://github.com/ccxt/ccxt
- **Python:** `ccxt`
- **Free tier:** да, бесплатная open-source библиотека.
- **Что даёт:** унифицированный API к 100+ биржам (Binance, Bybit, OKX,
  Coinbase, Kraken, Bitfinex, ...). Public endpoints без auth.
- **Качество:** ★★★★★ (де-факто стандарт).
- **Сложность:** ★☆☆☆☆ (~1 час).
- **Применение:** cross-exchange price/funding/oi sanity check.

### 4.3. CoinGecko API

- **URL:** https://www.coingecko.com/en/api
- **Free tier:** да, ~30 req/min без key, 500/min с key.
- **Что даёт:** prices, volumes, mcap для всех монет.
- **Качество:** ★★★★☆.
- **Сложность:** ★☆☆☆☆.
- **Применение:** для long-tail активов где Binance не покрывает.

---

## Раздел 5. ETF / institutional flows

### 5.1. SoSoValue (BTC/ETH spot ETF flows)

- **URL:** https://sosovalue.com/assets/etf/us-btc-spot
- **Free tier:** да, есть web-scraping endpoint.
- **Что даёт:** Daily net inflow по 11 BTC spot ETF (IBIT, FBTC, ...).
- **Качество:** ★★★★★ (official данные SEC EDGAR + bbtt).
- **Шум:** низкий.
- **Сложность:** ★★★☆☆ (scraping без официального API).
- **Применение:** ETF flow — один из лучших leading indicators для BTC
  с момента запуска spot-ETF (январь 2024).

### 5.2. BitMEX Research ETF data

- **URL:** https://blog.bitmex.com/research-bitcoin-etf-flows/
- **Free tier:** да, weekly CSV публикуют.
- **Качество:** ★★★★★.
- **Сложность:** ★★☆☆☆ (CSV download).

### 5.3. Farside Investors

- **URL:** https://farside.co.uk/btc/
- **Free tier:** scrapeable таблица.
- **Что даёт:** Daily BTC ETF flows + cumulative.
- **Качество:** ★★★★★.
- **Сложность:** ★★★☆☆ (web scraping).

---

## Раздел 6. Sentiment / альт. индикаторы

### 6.1. Alternative.me Fear & Greed (уже используется)

- **URL:** https://api.alternative.me/fng/
- **Free tier:** да, без ограничений.
- **Качество:** ★★★☆☆ (composite, не самый чистый сигнал).
- **Шум:** средний.
- **Текущий статус:** уже в системе. Hit-rate **0%** (упомянуто
  пользователем) — **PR #2 calibration** должен либо переоценить вес,
  либо вообще выкинуть.

### 6.2. Santiment

- **URL:** https://santiment.net/
- **Free tier:** очень ограниченный (только дельта-метрики, lag 24h).
- **Что даёт:**
  - Social Volume (упоминания в crypto twitter/reddit/telegram)
  - Dev Activity (github commits)
  - Holder distribution
  - On-chain metrics
- **Качество:** ★★★☆☆ (сильно шумно для intraday).
- **Сложность:** ★★★☆☆.
- **Решение:** pilot после калибровки on-chain метрик.

### 6.3. NLP/sentiment модели (FinBERT уже используем)

- **URL:** https://huggingface.co/ProsusAI/finbert
- **Free tier:** model open-source.
- **Качество:** ★★★☆☆.
- **Текущий статус:** в проекте используется. Hit-rate средний.
- **Что доделать:** добавить provenance — какой источник новости дал
  bullish/bearish score (Reuters / Bloomberg / Twitter / Reddit).
  Сейчас signal aggregated — нельзя понять что именно повлияло.

---

## Раздел 7. Эконом-календарь / events

### 7.1. ForexFactory / Investing.com calendar

- **URL:** https://www.forexfactory.com/calendar
- **Free tier:** scraping (нет официального API).
- **Что даёт:** macro events (NFP, CPI, FOMC, ECB) с фактом vs прогнозом.
- **Качество:** ★★★★☆.
- **Сложность:** ★★★★☆ (нестабильный scraping, anti-bot).
- **Альтернатива:** TradingEconomics API (paid после trial).
- **Применение:** event-defense — снижать risk перед NFP/CPI. У нас уже
  есть `core/event_defense.py`, можно расширить.

### 7.2. CoinMarketCal

- **URL:** https://coinmarketcal.com/
- **Free tier:** да, API key.
- **Что даёт:** крипто-события (token unlocks, mainnet launches, halvings).
- **Качество:** ★★★☆☆ (crowdsourced).

---

## Раздел 8. Реальный gap-анализ — куда движок сейчас слеп

Сравниваем что есть в `core/` с тем что **должно быть** на institutional
уровне:

| Категория | Сейчас в проекте | Gap | Приоритет |
|-----------|------------------|-----|-----------|
| **Микроструктура** | Нет orderbook depth, нет VPIN, нет Kyle's λ | ✗ Серьёзный | P2 (после MVP) |
| **Опционы** | Нет skew, нет GEX, нет DVOL | ✗ Серьёзный | **P0** (Deribit free) |
| **On-chain** | Нет SOPR / NUPL / MVRV / Exchange flows | ✗ Большой | **P0** (Glassnode free slice) |
| **Stablecoin supply** | Нет | ✗ Большой | **P0** (DefiLlama бесплатно) |
| **ETF flows** | Нет | ✗ Средний | P1 (SoSoValue scraping) |
| **Macro (FRED)** | Только VIX/DXY локальный fetch | ✗ Средний | P1 (FRED full) |
| **COT** | Есть, не калибровано | ✗ Калибровка | **P0** (PR #2 calibration) |
| **Funding/OI** | Есть Binance | OK | — |
| **Sentiment (FinBERT)** | Есть, шумно | ✗ Provenance | **P0** (PR #1) |

---

## Раздел 9. Что я бы добавил в backlog ПОСЛЕ PR #1 (provenance)

В порядке ROI (мокрая прикидка часов / impact):

### Wave 1 (после PR #1, ~2 недели)
1. **PR #2: Per-signal calibration** (10ч) — Brier score, reliability
   diagram, regime-conditional hit-rate. Решает 80% вопроса «какой сигнал
   работает».
2. **PR #3: Walk-forward backtest harness** (20ч) — rolling train/test,
   regime-stratified Sharpe/DD per signal.
3. **PR #4: DefiLlama stablecoin supply** (3ч) — feature `stables_inflow_7d`.

### Wave 2 (~1 месяц)
4. **PR #5: Deribit options features** (8ч) — DVOL, skew, put/call.
5. **PR #6: Glassnode free slice** (4ч) — SOPR, NUPL, Active Addr.
6. **PR #7: FRED macro features** (4ч) — DXY, M2, FFR, CPI.
7. **PR #8: Online adaptive weights** (15ч) — Bayesian Beta posteriors
   per signal-regime пары, shrinkage.

### Wave 3 (~2 месяца)
8. **PR #9: Factor model + risk parity** (20ч) — PCA decomposition, sizing.
9. **PR #10: Concept drift detection + kill switch** (10ч) — если recent
   Brier score дрейфует > 2σ от исторического → автоматически снижать size.
10. **PR #11: ETF flows scraper (SoSoValue + Farside)** (8ч).

### Wave 4 (post-MVP, для $10K MRR)
11. **PR #12: Kaiko orderbook (paid)** — VPIN, Kyle's λ, microstructure.
12. **PR #13: Multi-venue arbitrage detector** — cross-exchange edge.

---

## Раздел 10. Что НЕ стоит добавлять

Чтобы не размывать движок:

- ❌ **CryptoPanic, LunarCrush, TheTie** — sentiment-aggregator без provenance,
  на бэктестах не воспроизводится edge.
- ❌ **Twitter API** (X API v2) — $200/мес + анти-bot rules + noise.
  Лучше через FinBERT на готовых новостных лентах.
- ❌ **Whale Alert** — large transactions без context = шум.
- ❌ **Любые «AI-prediction» SaaS** (3Commas signals, Mudrex, CryptoHopper) —
  они тоже не знают, мы их сигнал не сможем калибровать.
- ❌ **Telegram-каналы / трейдеры** — нет provenance, нет track record.
- ❌ **DeepMind / Anthropic / OpenAI как «прогноз цены»** — это language models,
  не price predictors. Они уже у нас для debate/synth, не для price.

---

## Раздел 11. Регулятор / институциональный compliance

Если когда-нибудь продаём этот movement как сервис на западные деньги:
- **SEC**: registered investment advisor (RIA) registration — $10-50K юристы.
- **CFTC**: если торговать фьючерсами от лица клиентов — NFA registration.
- **Data licensing**: некоторые источники (Bloomberg, Refinitiv) требуют
  redistribution license.

**MVP:** не торгуем от лица клиентов, не даём «инвестиционные советы»,
формулировка «информационный инструмент для самостоятельного анализа».
Это покрывает 95% рисков для индивидуальной B2C-подписки.

---

## Итого

В backlog добавляю 11 PR (Wave 1-3) с конкретным source-mapping. Все источники
из Wave 1-2 — **бесплатные** или scrapeable. Wave 3 — pay-as-you-grow.

Главный вывод: **не нужно ещё 20 источников**. Нужно **per-signal calibration**
(PR #2) — без него любой новый источник усилит шум, а не edge.

После провенанса (PR #1) и калибровки (PR #2) мы впервые получим
объективный ответ на вопрос **«какой из имеющихся 15 сигналов реально
работает, какой шумит, какой контр-индикатор»**. Только после этого
имеет смысл подключать новые источники (FRED, Deribit, DefiLlama).
