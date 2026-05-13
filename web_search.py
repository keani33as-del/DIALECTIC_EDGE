"""
web_search.py — Реалтайм данные + реальный веб-поиск через Tavily.

Источники:
  BTC/ETH/SOL   → Binance + CoinGecko fallback
  S&P 500        → Yahoo ^GSPC (индекс, не ETF SPY)
  Nasdaq 100     → Yahoo ^NDX  (индекс, не ETF QQQ)
  VIX            → Yahoo ^VIX
  DXY            → Yahoo DX-Y.NYB
  Нефть WTI      → Yahoo CL=F + fallback BNO
  Золото         → Yahoo GC=F + XAUUSD=X fallback
  Макро          → FRED API (CPI пересчитывается в YoY %)
  Веб-новости    → Tavily API (реальный поиск, не DDG-пустышка)
  COT           → CFTC.gov (фьючерсное позиционирование)
  ETF Flows     → Yahoo (институциональные потоки)
"""

import asyncio
import logging
import os
import aiohttp
from datetime import datetime
from config import FRED_API_KEY

logger = logging.getLogger(__name__)

TIMEOUT       = aiohttp.ClientTimeout(total=15)
HEADERS       = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# ─── Константы ────────────────────────────────────────────────────────────────
CPI_BASE_YEAR_AGO    = 319.8   # CPI март 2025 по BLS → YoY ≈ 2.4%
FED_INFLATION_TARGET = 2.0

PRICE_SANITY = {
    "BTC":     (20_000,  250_000),
    "ETH":     (500,     30_000),
    "SOL":     (10,      3_000),
    "BNB":     (100,     5_000),
    "XRP":     (0.2,     50),
    "SPX":     (4_000,   12_000),
    "NDX":     (10_000,  35_000),
    "VIX":     (5,       90),
    "DXY":     (80,      130),
    "OIL_WTI": (30,      200),
    "GOLD":    (2_000,   8_000),
}

def _sane(key: str, price: float) -> bool:
    if not price or price <= 0:
        return False
    lo, hi = PRICE_SANITY.get(key, (0, 999_999_999))
    ok = lo <= price <= hi
    if not ok:
        logger.warning(f"[sanity] {key}: {price:.2f} вне [{lo}, {hi}]")
    return ok


# ─── Tavily — реальный веб-поиск ──────────────────────────────────────────────

async def search_tavily(query: str, max_results: int = 3) -> str:
    """
    Реальный поиск через Tavily API.
    Возвращает сниппеты новостей/аналитики для агентов.
    """
    if not TAVILY_API_KEY:
        return ""
    try:
        url = "https://api.tavily.com/search"
        payload = {
            "api_key":      TAVILY_API_KEY,
            "query":        query,
            "max_results":  max_results,
            "search_depth": "basic",
            "include_answer": True,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    parts = []
                    # Если есть прямой ответ — берём его
                    if data.get("answer"):
                        parts.append(f"[Ответ]: {data['answer'][:300]}")
                    # Плюс топ-3 результата
                    for res in data.get("results", [])[:max_results]:
                        title   = res.get("title", "")[:80]
                        content = res.get("content", "")[:200]
                        url_s   = res.get("url", "")
                        parts.append(f"• {title}\n  {content}\n  ({url_s})")
                    return "\n\n".join(parts)
    except Exception as e:
        logger.debug(f"Tavily '{query}': {e}")
    return ""


async def get_news_context(topics: list[str]) -> str:
    """
    Собирает свежие новости по списку тем через Tavily.
    Используется в run_full_analysis для обогащения контекста агентов.
    """
    if not TAVILY_API_KEY:
        return "⚠️ Tavily API не настроен — веб-поиск недоступен."

    queries = [
        "Fed interest rates inflation latest news today",
        "geopolitical risk oil markets today",
        "Bitcoin crypto market news today",
        "S&P 500 stock market outlook today",
    ]
    # Добавляем пользовательские темы
    for t in topics:
        if t:
            queries.append(f"{t} financial market impact today")

    tasks   = [search_tavily(q, max_results=2) for q in queries[:6]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    sections = []
    for q, res in zip(queries, results):
        if isinstance(res, str) and res.strip():
            sections.append(f"=== {q} ===\n{res}")

    if not sections:
        return "Свежих новостей не найдено."

    return "=== АКТУАЛЬНЫЕ НОВОСТИ (Tavily) ===\n\n" + "\n\n".join(sections)


async def search_news_context(topic: str) -> str:
    """Для /analyze — поиск по конкретной теме."""
    result = await search_tavily(f"{topic} market financial impact analysis", max_results=4)
    return result if result else "Свежих новостей по теме не найдено."


# ─── Binance ──────────────────────────────────────────────────────────────────

async def _binance(session, symbol: str, key: str) -> dict | None:
    try:
        url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
        async with session.get(url, timeout=TIMEOUT) as r:
            if r.status == 200:
                d      = await r.json()
                price  = float(d["lastPrice"])
                change = float(d["priceChangePercent"])
                volume = float(d.get("quoteVolume", 0))  # объём в USD
                if _sane(key, price):
                    return {
                        "price": price,
                        "change_24h": round(change, 3),
                        "volume_24h_usd": round(volume / 1_000_000, 1),  # в млн $
                        "source": "Binance"
                    }
    except Exception as e:
        logger.debug(f"Binance {symbol}: {e}")
    return None


async def _coingecko_crypto(session) -> dict:
    out = {}
    try:
        url    = "https://api.coingecko.com/api/v3/simple/price"
        params = {"ids": "bitcoin,ethereum,solana,binancecoin,ripple",
                  "vs_currencies": "usd",
                  "include_24hr_change": "true"}
        async with session.get(url, params=params, timeout=TIMEOUT) as r:
            if r.status == 200:
                data = await r.json()
                for cg_id, key in [("bitcoin","BTC"),("ethereum","ETH"),("solana","SOL"),
                                    ("binancecoin","BNB"),("ripple","XRP")]:
                    if cg_id in data:
                        p = float(data[cg_id].get("usd", 0))
                        c = float(data[cg_id].get("usd_24h_change", 0))
                        if _sane(key, p):
                            out[key] = {"price": p, "change_24h": round(c, 3),
                                        "source": "CoinGecko"}
    except Exception as e:
        logger.debug(f"CoinGecko crypto: {e}")
    return out




# ─── Тренд: 7d/30d изменение + MA50 + структура ──────────────────────────────

async def _fetch_trend_data(session, symbol_binance: str, key: str) -> dict:
    """
    Получает расширенные данные тренда через Binance klines API (бесплатно):
    - Изменение за 7 дней и 30 дней
    - MA50 (скользящая средняя 50 дней)
    - MA200 (скользящая средняя 200 дней)
    - Метка тренда: UPTREND / DOWNTREND / SIDEWAYS
    - Структура: HH/HL (Higher High/Higher Low) или LH/LL
    """
    result = {}
    try:
        # 200 дневных свечей достаточно для MA50 и MA200
        url = f"https://api.binance.com/api/v3/klines"
        params = {"symbol": symbol_binance, "interval": "1d", "limit": 200}
        async with session.get(url, params=params, timeout=TIMEOUT) as r:
            if r.status != 200:
                return result
            klines = await r.json()
            if not klines or len(klines) < 50:
                return result

        closes = [float(k[4]) for k in klines]  # индекс 4 = close price
        current = closes[-1]

        # Изменение за 7 и 30 дней
        if len(closes) >= 8:
            price_7d_ago = closes[-8]
            result["change_7d"] = round((current - price_7d_ago) / price_7d_ago * 100, 2)
        if len(closes) >= 31:
            price_30d_ago = closes[-31]
            result["change_30d"] = round((current - price_30d_ago) / price_30d_ago * 100, 2)

        # MA50 и MA200
        if len(closes) >= 50:
            ma50 = sum(closes[-50:]) / 50
            result["ma50"] = round(ma50, 2)
            result["above_ma50"] = current > ma50
        if len(closes) >= 200:
            ma200 = sum(closes[-200:]) / 200
            result["ma200"] = round(ma200, 2)
            result["above_ma200"] = current > ma200

        # Структура тренда — смотрим последние 14 свечей
        recent = closes[-14:]
        highs = [float(k[2]) for k in klines[-14:]]  # индекс 2 = high
        lows  = [float(k[3]) for k in klines[-14:]]  # индекс 3 = low

        # Считаем Higher Highs / Higher Lows
        hh = sum(1 for i in range(1, len(highs)) if highs[i] > highs[i-1])
        hl = sum(1 for i in range(1, len(lows))  if lows[i]  > lows[i-1])
        lh = sum(1 for i in range(1, len(highs)) if highs[i] < highs[i-1])
        ll = sum(1 for i in range(1, len(lows))  if lows[i]  < lows[i-1])

        # Определяем тренд
        bull_score = hh + hl
        bear_score = lh + ll
        above_ma50  = result.get("above_ma50", True)
        change_7d   = result.get("change_7d", 0)

        if bull_score > bear_score + 3 and above_ma50 and change_7d > 2:
            trend = "UPTREND"
            trend_emoji = "📈"
        elif bear_score > bull_score + 3 and not above_ma50 and change_7d < -2:
            trend = "DOWNTREND"
            trend_emoji = "📉"
        elif abs(change_7d) < 3 and abs(result.get("change_30d", 0)) < 8:
            trend = "SIDEWAYS"
            trend_emoji = "↔️"
        elif change_7d > 0 and above_ma50:
            trend = "UPTREND"
            trend_emoji = "📈"
        elif change_7d < 0 and not above_ma50:
            trend = "DOWNTREND"
            trend_emoji = "📉"
        else:
            trend = "SIDEWAYS"
            trend_emoji = "↔️"

        # ── ATR(14d) — Average True Range для SL-guard (pre-live-hardening) ──
        # True Range = max(high-low, |high-prev_close|, |low-prev_close|)
        if len(klines) >= 15:
            true_ranges = []
            for i in range(-14, 0):
                h = float(klines[i][2])
                l = float(klines[i][3])
                prev_c = float(klines[i - 1][4])
                tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
                true_ranges.append(tr)
            atr_14 = sum(true_ranges) / len(true_ranges)
            result["atr_14d"] = round(atr_14, 2)
            result["atr_14d_pct"] = round(atr_14 / current * 100, 2)

        result["trend"] = trend
        result["trend_emoji"] = trend_emoji
        result["hh"] = hh
        result["hl"] = hl
        result["lh"] = lh
        result["ll"] = ll

    except Exception as e:
        logger.debug(f"Trend data {symbol_binance}: {e}")
    return result


# ─── Yahoo Finance ────────────────────────────────────────────────────────────

async def _fetch_yahoo_trend(session, ticker: str, key: str) -> dict:
    """MA50/MA200/ATR(14d) для макро-активов через Yahoo Finance.

    Используется для SPX/NDX/VIX/DXY/OIL/GOLD — у крипты MA-метрики уже
    приходят из Binance через `_fetch_trend_data`. Аналогичная структура
    результата: `ma50`, `ma200`, `above_ma50`, `above_ma200`, `atr_14d`,
    `change_7d`, `change_30d`, `trend`/`trend_emoji`.

    Yahoo `range=1y` → ~250 дневных свечей → MA200 считается корректно.
    """
    result = {}
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        async with session.get(url, params={"interval": "1d", "range": "1y"},
                               timeout=TIMEOUT) as r:
            if r.status != 200:
                return result
            data = await r.json()
            chart = data.get("chart") or {}
            results = chart.get("result") or []
            if not results:
                return result
            r0 = results[0]
            ind = r0.get("indicators") or {}
            quotes = (ind.get("quote") or [{}])[0]
            closes_raw = quotes.get("close") or []
            highs_raw = quotes.get("high") or []
            lows_raw = quotes.get("low") or []
            # Yahoo может вернуть None для нерабочих дней
            closes = [c for c in closes_raw if c is not None]
            highs = [h for h in highs_raw if h is not None]
            lows = [l for l in lows_raw if l is not None]
            if len(closes) < 50:
                return result
            current = closes[-1]

            if len(closes) >= 8:
                result["change_7d"] = round((current - closes[-8]) / closes[-8] * 100, 2)
            if len(closes) >= 31:
                result["change_30d"] = round((current - closes[-31]) / closes[-31] * 100, 2)

            if len(closes) >= 50:
                ma50 = sum(closes[-50:]) / 50
                result["ma50"] = round(ma50, 2)
                result["above_ma50"] = current > ma50
            if len(closes) >= 200:
                ma200 = sum(closes[-200:]) / 200
                result["ma200"] = round(ma200, 2)
                result["above_ma200"] = current > ma200

            # ATR(14d) — нужно совпадение длин закрытий с high/low
            if len(closes) >= 15 and len(highs) >= 15 and len(lows) >= 15:
                # Сводим списки к одинаковой длине (берём последние 15)
                n = min(len(closes), len(highs), len(lows))
                if n >= 15:
                    cs = closes_raw[-n:]
                    hs = highs_raw[-n:]
                    ls = lows_raw[-n:]
                    trs = []
                    for i in range(-14, 0):
                        try:
                            h = float(hs[i]); l = float(ls[i]); prev_c = float(cs[i - 1])
                            trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
                        except (TypeError, ValueError):
                            continue
                    if trs:
                        atr_14 = sum(trs) / len(trs)
                        result["atr_14d"] = round(atr_14, 4)
                        result["atr_14d_pct"] = round(atr_14 / current * 100, 2)

            # Простой trend label (как в _fetch_trend_data, но без HH/HL)
            ch_7d = result.get("change_7d", 0) or 0
            ch_30d = result.get("change_30d", 0) or 0
            above_ma50 = result.get("above_ma50", True)
            if ch_7d > 2 and above_ma50:
                result["trend"], result["trend_emoji"] = "UPTREND", "📈"
            elif ch_7d < -2 and not above_ma50:
                result["trend"], result["trend_emoji"] = "DOWNTREND", "📉"
            elif abs(ch_7d) < 3 and abs(ch_30d) < 8:
                result["trend"], result["trend_emoji"] = "SIDEWAYS", "↔️"
            elif ch_7d >= 0:
                result["trend"], result["trend_emoji"] = "UPTREND", "📈"
            else:
                result["trend"], result["trend_emoji"] = "DOWNTREND", "📉"

    except Exception as e:
        logger.debug(f"Yahoo trend {ticker}: {e}")
    return result


async def _yahoo(session, ticker: str, key: str) -> dict | None:
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        async with session.get(url, params={"interval": "1d", "range": "5d"},
                               timeout=TIMEOUT) as r:
            if r.status == 200:
                data   = await r.json()
                result = data["chart"]["result"][0]
                meta   = result["meta"]
                price  = float(meta.get("regularMarketPrice", 0))
                
                change = 0.0
                if result.get("indicators", {}).get("quote"):
                    quotes = result["indicators"]["quote"][0]
                    closes = quotes.get("close", [])
                    if len(closes) >= 2 and closes[-1] is not None and closes[-2] is not None:
                        prev_price = closes[-2]
                        change = ((price - prev_price) / prev_price * 100) if prev_price else 0.0
                    elif len(closes) >= 1 and closes[-1] is not None:
                        prev = float(meta.get("previousClose", price) or price)
                        if prev != price:
                            change = ((price - prev) / prev * 100) if prev else 0.0
                
                if _sane(key, price):
                    return {"price": round(price, 2),
                            "change_24h": round(change, 3),
                            "source": f"Yahoo ({ticker})"}
    except Exception as e:
        logger.debug(f"Yahoo {ticker}: {e}")
    return None


async def _gold(session) -> dict | None:
    """GC=F → XAUUSD=X → предупреждение."""
    for ticker in ["GC=F", "XAUUSD=X"]:
        r = await _yahoo(session, ticker, "GOLD")
        if r:
            logger.info(f"Золото {ticker}: ${r['price']:,.2f}")
            return r
    logger.warning("Золото: все источники недоступны")
    return None


async def _oil(session) -> dict | None:
    """CL=F → BNO → предупреждение."""
    for ticker, key in [("CL=F", "OIL_WTI"), ("BNO", "OIL_WTI")]:
        r = await _yahoo(session, ticker, key)
        if r:
            return r
    return None


# ─── FRED ─────────────────────────────────────────────────────────────────────

async def _fred(session, series_id: str) -> str:
    if not FRED_API_KEY or FRED_API_KEY in ("", "твой_ключ", "YOUR_KEY"):
        return "N/A"
    try:
        url    = "https://api.stlouisfed.org/fred/series/observations"
        params = {"series_id": series_id, "api_key": FRED_API_KEY,
                  "file_type": "json", "limit": 1, "sort_order": "desc"}
        async with session.get(url, params=params, timeout=TIMEOUT) as r:
            if r.status == 200:
                data = await r.json()
                val  = data["observations"][0]["value"]
                return val if val != "." else "N/A"
    except Exception as e:
        logger.debug(f"FRED {series_id}: {e}")
    return "N/A"


async def _fear_greed(session) -> dict:
    try:
        async with session.get("https://api.alternative.me/fng/?limit=8",
                               timeout=TIMEOUT) as r:
            if r.status == 200:
                d     = await r.json()
                items = d.get("data", [])
                if items:
                    cur  = items[0]
                    prev = int(items[1]["value"]) if len(items) > 1 else int(cur["value"])
                    val  = int(cur["value"])
                    # 7-дневный тренд F&G (pre-live-hardening)
                    history_7d = [int(it["value"]) for it in items[:7] if it.get("value")]
                    trend_7d = ""
                    if len(history_7d) >= 5:
                        avg_recent = sum(history_7d[:3]) / 3
                        avg_older = sum(history_7d[4:7]) / max(len(history_7d[4:7]), 1)
                        if avg_recent > avg_older + 5:
                            trend_7d = "RISING"
                        elif avg_recent < avg_older - 5:
                            trend_7d = "FALLING"
                        else:
                            trend_7d = "STABLE"
                    return {"val": val, "status": cur["value_classification"],
                            "change": val - prev, "trend_7d": trend_7d,
                            "history_7d": history_7d}
    except Exception as e:
        logger.debug(f"F&G: {e}")
    return {"val": "N/A", "status": "Unknown", "change": 0, "trend_7d": "", "history_7d": []}


# ─── Агрегатор ────────────────────────────────────────────────────────────────

async def fetch_realtime_prices() -> dict:
    prices = {}
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        (btc, eth, sol, bnb, xrp,
         spx, ndx, vix, dxy, oil, gold,
         fed_rate, cpi_raw, fng,
         trend_btc, trend_eth, trend_sol, trend_bnb, trend_xrp,
         trend_spx, trend_ndx, trend_vix, trend_dxy, trend_oil, trend_gold) = await asyncio.gather(
            _binance(session, "BTCUSDT", "BTC"),
            _binance(session, "ETHUSDT", "ETH"),
            _binance(session, "SOLUSDT", "SOL"),
            _binance(session, "BNBUSDT", "BNB"),
            _binance(session, "XRPUSDT", "XRP"),
            _yahoo(session, "^GSPC",    "SPX"),
            _yahoo(session, "^NDX",     "NDX"),
            _yahoo(session, "^VIX",     "VIX"),
            _yahoo(session, "DX-Y.NYB", "DXY"),
            _oil(session),
            _gold(session),
            _fred(session, "FEDFUNDS"),
            _fred(session, "CPIAUCSL"),
            _fear_greed(session),
            _fetch_trend_data(session, "BTCUSDT", "BTC"),
            _fetch_trend_data(session, "ETHUSDT", "ETH"),
            _fetch_trend_data(session, "SOLUSDT", "SOL"),
            _fetch_trend_data(session, "BNBUSDT", "BNB"),
            _fetch_trend_data(session, "XRPUSDT", "XRP"),
            _fetch_yahoo_trend(session, "^GSPC",    "SPX"),
            _fetch_yahoo_trend(session, "^NDX",     "NDX"),
            _fetch_yahoo_trend(session, "^VIX",     "VIX"),
            _fetch_yahoo_trend(session, "DX-Y.NYB", "DXY"),
            _fetch_yahoo_trend(session, "CL=F",     "OIL_WTI"),
            _fetch_yahoo_trend(session, "GC=F",     "GOLD"),
            return_exceptions=True,
        )

        # Крипта с fallback
        missing = []
        for key, val in [("BTC", btc), ("ETH", eth), ("SOL", sol),
                         ("BNB", bnb), ("XRP", xrp)]:
            if val and not isinstance(val, Exception):
                prices[key] = val
            else:
                missing.append(key)
        if missing:
            cg = await _coingecko_crypto(session)
            for k in missing:
                if k in cg:
                    prices[k] = cg[k]

        # Добавляем тренд+MA к крипто данным
        for key, trend_val in [("BTC", trend_btc), ("ETH", trend_eth), ("SOL", trend_sol),
                                ("BNB", trend_bnb), ("XRP", trend_xrp)]:
            if key in prices and trend_val and not isinstance(trend_val, Exception) and trend_val:
                prices[key].update(trend_val)
                # Прокидываем ATR в top-level для SL-guard (market_prices["ATR_BTC"])
                if "atr_14d" in trend_val:
                    prices[f"ATR_{key}"] = trend_val["atr_14d"]
                # Прокидываем MA200/MA50 в top-level — Synth use'ит их в plans
                if "ma200" in trend_val:
                    prices[f"MA200_{key}"] = trend_val["ma200"]
                if "ma50" in trend_val:
                    prices[f"MA50_{key}"] = trend_val["ma50"]

        # Макро: prices + MA-тренд
        for key, val in [("SPX", spx), ("NDX", ndx), ("VIX", vix),
                         ("DXY", dxy), ("OIL_WTI", oil), ("GOLD", gold)]:
            if val and not isinstance(val, Exception):
                prices[key] = val
        for key, trend_val in [("SPX", trend_spx), ("NDX", trend_ndx), ("VIX", trend_vix),
                                ("DXY", trend_dxy), ("OIL_WTI", trend_oil), ("GOLD", trend_gold)]:
            if key in prices and trend_val and not isinstance(trend_val, Exception) and trend_val:
                prices[key].update(trend_val)
                if "ma200" in trend_val:
                    prices[f"MA200_{key}"] = trend_val["ma200"]
                if "ma50" in trend_val:
                    prices[f"MA50_{key}"] = trend_val["ma50"]
                if "atr_14d" in trend_val:
                    prices[f"ATR_{key}"] = trend_val["atr_14d"]

        prices["MACRO"] = {
            "fed_rate": fed_rate if not isinstance(fed_rate, Exception) else "N/A",
            "cpi_raw":  cpi_raw  if not isinstance(cpi_raw, Exception)  else "N/A",
            "fng":      fng      if not isinstance(fng, Exception) else
                        {"val": "N/A", "status": "Unknown", "change": 0},
        }

    got     = [k for k in prices if k != "MACRO"]
    missing = [k for k in ["BTC","ETH","SPX","NDX","GOLD","OIL_WTI"] if k not in prices]
    logger.info(f"✅ Цены: {got}")
    if missing:
        logger.warning(f"❌ Не получены: {missing}")
    return prices


# ─── CPI → YoY % ──────────────────────────────────────────────────────────────

def _cpi_yoy(raw: str) -> str:
    try:
        v   = float(raw)
        yoy = (v - CPI_BASE_YEAR_AGO) / CPI_BASE_YEAR_AGO * 100
        gap = yoy - FED_INFLATION_TARGET
        g_s = f"+{gap:.1f}%" if gap > 0 else f"{gap:.1f}%"
        status = ("выше таргета" if gap > 1.0 else
                  "незначительно выше таргета" if gap > 0.3 else
                  "близко к таргету")
        return f"~{yoy:.1f}% YoY — {status} (таргет 2.0%, отклонение {g_s})"
    except (ValueError, TypeError):
        return "нет данных"


# ─── Форматирование для агентов ───────────────────────────────────────────────

def format_prices_for_agents(prices: dict) -> str:
    if not prices:
        return "Рыночные данные временно недоступны."

    now   = datetime.now().strftime("%d.%m.%Y %H:%M UTC")
    lines = [f"=== ВЕРИФИЦИРОВАННЫЕ РЫНОЧНЫЕ ДАННЫЕ ({now}) ==="]

    lines.append("\n[КРИПТОРЫНОК]")
    for k, label in [("BTC","Bitcoin"),("ETH","Ethereum"),("SOL","Solana"),
                     ("BNB","BNB"),("XRP","XRP")]:
        if k in prices:
            p  = prices[k]
            ch = p["change_24h"]
            ch7  = p.get("change_7d")
            ch30 = p.get("change_30d")
            ma50 = p.get("ma50")
            ma200 = p.get("ma200")
            above50  = p.get("above_ma50")
            above200 = p.get("above_ma200")
            trend    = p.get("trend", "")
            trend_e  = p.get("trend_emoji", "")
            vol      = p.get("volume_24h_usd")

            # Базовая строка
            line = (f"  {label} ({k}): ${p['price']:,.2f}  "
                    f"{'🟢' if ch>=0 else '🔴'} {ch:+.2f}% (24ч)")
            if ch7 is not None:
                line += f"  {'🟢' if ch7>=0 else '🔴'} {ch7:+.1f}% (7д)"
            if ch30 is not None:
                line += f"  {'🟢' if ch30>=0 else '🔴'} {ch30:+.1f}% (30д)"
            line += f"  [{p['source']}]"
            lines.append(line)

            # Тренд и MA
            if trend:
                trend_line = f"    {trend_e} ТРЕНД: {trend}"
                if ma50 is not None:
                    pos50 = "выше" if above50 else "ниже"
                    pct50 = ((p['price'] - ma50) / ma50 * 100)
                    trend_line += f" | MA50: ${ma50:,.0f} ({pos50}, {pct50:+.1f}%)"
                if ma200 is not None:
                    pos200 = "выше" if above200 else "ниже"
                    pct200 = ((p['price'] - ma200) / ma200 * 100)
                    trend_line += f" | MA200: ${ma200:,.0f} ({pos200}, {pct200:+.1f}%)"
                lines.append(trend_line)

            # Объём
            if vol:
                lines.append(f"    Объём 24ч: ${vol:,.0f}M USD")

    if "MACRO" in prices:
        m   = prices["MACRO"]
        fng = m.get("fng", {})
        fv, fs = fng.get("val","N/A"), fng.get("status","")
        fc = fng.get("change", 0)
        lines.append("\n[МАКРОЭКОНОМИКА США]")
        lines.append(f"  Ставка ФРС:   {m['fed_rate']}%  [FRED]")
        lines.append(f"  Инфляция CPI: {_cpi_yoy(m.get('cpi_raw','N/A'))}  [FRED]")
        lines.append(
            f"  Fear & Greed: {fv}/100 ({fs})  "
            f"{'🟢' if fc > 0 else '🔴' if fc < 0 else '➡️'} {fc:+d} за сутки  "
            f"[Источник: Alternative.me Crypto F&G — НЕ FRED]"
        )
        # F&G 7-дневный тренд (pre-live-hardening)
        fng_trend = fng.get("trend_7d", "")
        fng_hist = fng.get("history_7d", [])
        if fng_trend and fng_hist:
            trend_label = {"RISING": "↗️ растёт", "FALLING": "↘️ падает", "STABLE": "→ стабилен"}.get(fng_trend, fng_trend)
            lines.append(f"  F&G тренд 7д: {trend_label} ({' → '.join(str(x) for x in fng_hist[:5])})")
        lines.append("  [!] CPI = индекс (~323), НЕ %. Инфляция = YoY (выше).")

    # MA200/MA50 для макро — нужны Synth-агенту, чтобы строить per-asset планы
    # с двумя триггерами (закрытие выше MA200 → LONG; ниже MA50 → SHORT).
    def _macro_trend_line(p: dict, unit: str = "") -> str:
        ma50 = p.get("ma50"); ma200 = p.get("ma200")
        above50 = p.get("above_ma50"); above200 = p.get("above_ma200")
        trend = p.get("trend", ""); trend_e = p.get("trend_emoji", "")
        chunks = []
        if trend:
            chunks.append(f"{trend_e} ТРЕНД: {trend}")
        if ma50 is not None:
            pos50 = "выше" if above50 else "ниже"
            pct50 = ((p['price'] - ma50) / ma50 * 100) if ma50 else 0
            chunks.append(f"MA50: {ma50:,.2f}{unit} ({pos50}, {pct50:+.1f}%)")
        if ma200 is not None:
            pos200 = "выше" if above200 else "ниже"
            pct200 = ((p['price'] - ma200) / ma200 * 100) if ma200 else 0
            chunks.append(f"MA200: {ma200:,.2f}{unit} ({pos200}, {pct200:+.1f}%)")
        return " | ".join(chunks)

    lines.append("\n[ФОНДОВЫЕ ИНДЕКСЫ]")
    for k, label in [("SPX","S&P 500"),("NDX","Nasdaq 100"),("VIX","VIX")]:
        if k in prices:
            p  = prices[k]
            ch = p["change_24h"]
            lines.append(f"  {label}: {p['price']:,.2f}  "
                         f"{'🟢' if ch>=0 else '🔴'} {ch:+.2f}%  [{p['source']}]")
            tl = _macro_trend_line(p)
            if tl:
                lines.append(f"    {tl}")

    lines.append("\n[СЫРЬЁ И ВАЛЮТЫ]")
    for k, label, unit in [("OIL_WTI","Нефть WTI","$/барр"),
                            ("GOLD","Золото","$/унц"),
                            ("DXY","Индекс доллара","")]:
        if k in prices:
            p  = prices[k]
            ch = p["change_24h"]
            u  = f" {unit}" if unit else ""
            lines.append(f"  {label}: {p['price']:,.2f}{u}  "
                         f"{'🟢' if ch>=0 else '🔴'} {ch:+.2f}%  [{p['source']}]")
            tl = _macro_trend_line(p, unit=u if k == "OIL_WTI" or k == "GOLD" else "")
            if tl:
                lines.append(f"    {tl}")

    lines.append("\n⚠️ ИНСТРУКЦИЯ: используй ТОЛЬКО эти цифры. "
                 "Если актива нет — пиши 'нет данных'.")
    return "\n".join(lines)


async def get_full_realtime_context() -> tuple[dict, str]:
    prices    = await fetch_realtime_prices()
    formatted = format_prices_for_agents(prices)
    
    try:
        from cot_data import format_cot_for_agents, get_cot_for_assets
        cot_data = await get_cot_for_assets(["Bitcoin", "Gold", "Crude Oil"])
        cot_formatted = format_cot_for_agents(cot_data)
        formatted += "\n\n" + cot_formatted
    except Exception as e:
        logger.warning(f"COT data error: {e}")
    
    try:
        from etf_flows import format_etf_flows_for_agents, get_market_breadth, get_etf_flows
        etf_data = await get_etf_flows()
        etf_formatted = format_etf_flows_for_agents(etf_data)
        formatted += "\n\n" + etf_formatted
        
        breadth = await get_market_breadth()
        prices["BREADTH"] = breadth
    except Exception as e:
        logger.warning(f"ETF flows error: {e}")
    
    return prices, formatted
