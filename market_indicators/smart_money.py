"""
smart_money.py — институциональные / smart-money сигналы.

Идея: ритейл-трейдеры в массе своей убыточны, поэтому полезнее смотреть
не на «глобальный long/short ratio» (= ритейл), а на то, что делают
ТОП-трейдеры, US-институционалы и фьючерсные хеджеры.

Сигналы:
1. **Top-trader L/S position ratio** (Binance Futures)
   — что сделают КРУПНЫЕ позиции (топ 20% аккаунтов по марже).
   Положительная дивергенция с ритейлом = бычий сигнал.

2. **Coinbase Premium**
   — (Coinbase USD spot − Binance USDT spot) / Binance USDT spot.
   Положительный = US-институционалы агрессивно покупают
   (Coinbase = главный on-ramp для американских фондов и ETF).

3. **CME Basis**
   — (CME BTC=F front-month − BTC spot) / BTC spot.
   Положительный = институционалы платят премию за фьючерс →
   бычьи ожидания. Отрицательный = «backwardation» → стресс.

4. **Funding Rate Dispersion**
   — funding по BTC, ETH, SOL, BNB, XRP.
   Если ВСЕ отрицательные = ритейл массово в шорте,
   высокий риск short-squeeze → contrarian-бычий.
   Если ВСЕ положительные сильно (>0.05%) = перегрев лонгов.

Все источники бесплатные и не требуют ключей.

Fallback: если эндпоинт недоступен (например, Binance Futures
geo-block в локальном dev-окружении), возвращаем None по этому
полю — модуль остаётся функционален.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# ── Endpoints ──────────────────────────────────────────────────────────────────
# Futures (geo-restricted). Если не работает — мы возвращаем None, модуль
# остаётся работоспособным.
BINANCE_FUTURES_BASE = "https://fapi.binance.com"
# Spot data-api.binance.vision доступен из geo-restricted регионов (используем
# как fallback и для расчёта Coinbase Premium).
BINANCE_SPOT_BASE = "https://data-api.binance.vision"
COINBASE_BASE = "https://api.coinbase.com"
YAHOO_QUOTE_BASE = "https://query1.finance.yahoo.com"

USER_AGENT = "Mozilla/5.0 (compatible; dialectic-edge/1.0)"
TIMEOUT = aiohttp.ClientTimeout(total=8)

DEFAULT_FUNDING_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
# Топ-трейдер L/S тянем по тем же 5 основным парам.
DEFAULT_LS_SYMBOLS = DEFAULT_FUNDING_SYMBOLS


@dataclass
class SmartMoneySignals:
    """Институциональные / smart-money сигналы."""

    # 1. Top-trader L/S position ratio (Binance)
    # >1 = топ-трейдеры в лонге, <1 = в шорте.
    # `top_trader_ls_ratio` — BTC (бек-совместимость).
    # `top_trader_ls_per_symbol` — dict по 5 основным парам.
    top_trader_ls_ratio: Optional[float] = None
    top_trader_ls_signal: str = "N/A"
    top_trader_ls_per_symbol: dict = field(default_factory=dict)  # {symbol: ratio}

    # 2. Coinbase Premium (% spread Coinbase USD vs Binance USDT)
    # Положительный = US institutional bid pressure.
    coinbase_premium_pct: Optional[float] = None
    coinbase_premium_signal: str = "N/A"
    coinbase_price_usd: Optional[float] = None
    binance_price_usdt: Optional[float] = None

    # 3. CME Basis (% premium CME BTC=F vs spot)
    # Положительный = institutional contango bullishness.
    cme_basis_pct: Optional[float] = None
    cme_basis_signal: str = "N/A"
    cme_front_price: Optional[float] = None
    spot_price_for_basis: Optional[float] = None

    # 4. Funding rate dispersion
    funding_rates: dict = field(default_factory=dict)  # {symbol: pct}
    funding_avg_pct: Optional[float] = None
    funding_dispersion_signal: str = "N/A"
    funding_alignment: Optional[str] = None  # "ALL_LONG" / "ALL_SHORT" / "MIXED"


# ── Helpers ────────────────────────────────────────────────────────────────────


async def _get_json(session: aiohttp.ClientSession, url: str, params: dict | None = None) -> Optional[dict | list]:
    try:
        async with session.get(url, params=params, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}) as r:
            if r.status != 200:
                logger.debug(f"[SMART-MONEY] {url} HTTP {r.status}")
                return None
            return await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.debug(f"[SMART-MONEY] {url} fetch error: {e}")
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[SMART-MONEY] {url} unexpected error: {e}")
        return None


# ── Fetchers ──────────────────────────────────────────────────────────────────


async def fetch_top_trader_ls(session: aiohttp.ClientSession, symbol: str = "BTCUSDT") -> Optional[float]:
    """Top trader long/short POSITION ratio (Binance Futures).

    Returns ratio (long_position / short_position). >1 = топ-трейдеры лонг.

    Эндпоинт geo-restricted (требует доступ к fapi.binance.com).
    """
    url = f"{BINANCE_FUTURES_BASE}/futures/data/topLongShortPositionRatio"
    data = await _get_json(session, url, {"symbol": symbol, "period": "1d", "limit": 1})
    if not data or not isinstance(data, list):
        return None
    try:
        # Самая свежая запись — последняя
        ratio = float(data[-1].get("longShortRatio", 0) or 0)
        if ratio <= 0:
            return None
        return ratio
    except (ValueError, TypeError, KeyError, IndexError):
        return None


async def fetch_coinbase_spot(session: aiohttp.ClientSession, pair: str = "BTC-USD") -> Optional[float]:
    """Coinbase USD spot price."""
    url = f"{COINBASE_BASE}/v2/prices/{pair}/spot"
    data = await _get_json(session, url)
    if not data:
        return None
    try:
        return float(data["data"]["amount"])
    except (KeyError, ValueError, TypeError):
        return None


async def fetch_binance_spot(session: aiohttp.ClientSession, symbol: str = "BTCUSDT") -> Optional[float]:
    """Binance spot price via data-api.binance.vision (доступен из geo-restricted)."""
    url = f"{BINANCE_SPOT_BASE}/api/v3/ticker/price"
    data = await _get_json(session, url, {"symbol": symbol})
    if not data:
        return None
    try:
        return float(data.get("price", 0) or 0) or None
    except (ValueError, TypeError):
        return None


async def fetch_yahoo_quote(session: aiohttp.ClientSession, symbol: str) -> Optional[float]:
    """Yahoo Finance quote (regularMarketPrice)."""
    url = f"{YAHOO_QUOTE_BASE}/v8/finance/chart/{symbol}"
    data = await _get_json(session, url, {"interval": "1d", "range": "5d"})
    if not data:
        return None
    try:
        meta = data["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        return float(price) if price is not None else None
    except (KeyError, ValueError, TypeError, IndexError):
        return None


async def fetch_funding_rate(session: aiohttp.ClientSession, symbol: str) -> Optional[float]:
    """Текущая funding rate в долях (например 0.0001 = +0.01%)."""
    url = f"{BINANCE_FUTURES_BASE}/fapi/v1/premiumIndex"
    data = await _get_json(session, url, {"symbol": symbol})
    if not data:
        return None
    try:
        return float(data.get("lastFundingRate", 0) or 0)
    except (ValueError, TypeError):
        return None


# ── Signal classification ─────────────────────────────────────────────────────


def _classify_top_trader(ratio: float) -> str:
    """ratio = long_position / short_position у топ-трейдеров."""
    if ratio >= 2.0:
        return "🟢 Сильно лонг (топ-трейдеры в bullish позициях)"
    if ratio >= 1.3:
        return "🟢 Лонг (топ-трейдеры в bullish позициях)"
    if ratio >= 0.85:
        return "⚪ Нейтрально (топ-трейдеры сбалансированы)"
    if ratio >= 0.6:
        return "🔴 Шорт (топ-трейдеры в bearish позициях)"
    return "🔴 Сильно шорт (топ-трейдеры в bearish позициях)"


def _classify_coinbase_premium(pct: float) -> str:
    """premium в %. Положительный = US institutional buy pressure."""
    if pct >= 0.30:
        return "🟢 Сильная US institutional покупка"
    if pct >= 0.10:
        return "🟢 US institutional buy pressure"
    if pct >= -0.10:
        return "⚪ US institutionals нейтральны"
    if pct >= -0.30:
        return "🔴 US institutional sell pressure"
    return "🔴 Сильная US institutional распродажа"


def _classify_cme_basis(pct: float) -> str:
    """basis в %. annualised премия фьючерса над спотом."""
    if pct >= 0.5:
        return "🟢 Сильная институциональная контанго (бычий)"
    if pct >= 0.1:
        return "🟢 Институциональная контанго (бычий)"
    if pct >= -0.1:
        return "⚪ Базис нейтрален"
    if pct >= -0.5:
        return "🔴 Backwardation (медвежий)"
    return "🔴 Сильная backwardation (стресс)"


def _classify_funding_dispersion(rates: dict) -> tuple[Optional[float], str, Optional[str]]:
    """Анализ funding по 5 парам.

    Returns: (avg_pct, signal, alignment)
    - alignment: "ALL_LONG" / "ALL_SHORT" / "MIXED"
    """
    if not rates:
        return None, "N/A", None

    values = [v for v in rates.values() if v is not None]
    if not values:
        return None, "N/A", None

    avg = sum(values) / len(values) * 100  # в %
    pos = sum(1 for v in values if v > 0)
    neg = sum(1 for v in values if v < 0)

    if pos == len(values):
        alignment = "ALL_LONG"
    elif neg == len(values):
        alignment = "ALL_SHORT"
    else:
        alignment = "MIXED"

    if alignment == "ALL_SHORT" and avg < -0.005:
        signal = (f"🟢 Funding {avg:+.4f}% — все 5 пар в шорте → "
                  f"риск short-squeeze (contrarian-бычий)")
    elif alignment == "ALL_LONG" and avg > 0.05:
        signal = (f"🔴 Funding {avg:+.4f}% — все пары в перегретом лонге → "
                  f"риск long-squeeze")
    elif alignment == "ALL_LONG" and avg > 0.01:
        signal = f"🟢 Funding {avg:+.4f}% — широкое лонг-настроение"
    elif alignment == "ALL_SHORT":
        signal = f"⚪ Funding {avg:+.4f}% — все в шорте, но умеренно"
    else:
        signal = f"⚪ Funding {avg:+.4f}% — смешанный (нет единого настроения)"

    return avg, signal, alignment


# ── Main fetcher ──────────────────────────────────────────────────────────────


async def fetch_smart_money_signals(symbol_btc: str = "BTCUSDT") -> SmartMoneySignals:
    """Собирает все smart-money сигналы параллельно."""
    signals = SmartMoneySignals()

    async with aiohttp.ClientSession() as session:
        # Параллельно запускаем все запросы
        tasks: dict = {
            "coinbase_btc": fetch_coinbase_spot(session, "BTC-USD"),
            "binance_btc": fetch_binance_spot(session, "BTCUSDT"),
            "cme_btc": fetch_yahoo_quote(session, "BTC=F"),
            "spot_btc_yahoo": fetch_yahoo_quote(session, "BTC-USD"),
        }
        # Top-trader L/S по 5 основным парам (было только BTC).
        for sym in DEFAULT_LS_SYMBOLS:
            tasks[f"ls_{sym}"] = fetch_top_trader_ls(session, sym)
        # Funding rates по 5 парам
        for sym in DEFAULT_FUNDING_SYMBOLS:
            tasks[f"funding_{sym}"] = fetch_funding_rate(session, sym)

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        out = dict(zip(tasks.keys(), results))

    def _v(k):
        v = out.get(k)
        if isinstance(v, Exception):
            logger.debug(f"[SMART-MONEY] {k} raised: {v}")
            return None
        return v

    # 1. Top-trader L/S — берём по всем 5 парам, BTC дублируем в legacy-поле.
    per_symbol_ls: dict[str, float] = {}
    for sym in DEFAULT_LS_SYMBOLS:
        v = _v(f"ls_{sym}")
        if v is not None:
            per_symbol_ls[sym] = round(float(v), 2)
    signals.top_trader_ls_per_symbol = per_symbol_ls
    ls = per_symbol_ls.get(symbol_btc)
    if ls is not None:
        signals.top_trader_ls_ratio = ls
        signals.top_trader_ls_signal = _classify_top_trader(ls)
        logger.info(
            f"[SMART-MONEY] Top-trader L/S BTC={ls:.2f}, "
            f"all={ {k: v for k, v in per_symbol_ls.items()} }"
        )

    # 2. Coinbase Premium
    cb = _v("coinbase_btc")
    bn = _v("binance_btc")
    if cb and bn and bn > 0:
        premium = (cb - bn) / bn * 100
        signals.coinbase_premium_pct = round(premium, 3)
        signals.coinbase_premium_signal = _classify_coinbase_premium(premium)
        signals.coinbase_price_usd = round(cb, 2)
        signals.binance_price_usdt = round(bn, 2)
        logger.info(f"[SMART-MONEY] Coinbase Premium: {premium:+.3f}% (CB={cb}, BIN={bn})")

    # 3. CME Basis (Yahoo BTC=F vs spot proxy)
    cme = _v("cme_btc")
    spot_for_basis = bn or _v("spot_btc_yahoo") or cb
    if cme and spot_for_basis and spot_for_basis > 0:
        basis = (cme - spot_for_basis) / spot_for_basis * 100
        signals.cme_basis_pct = round(basis, 3)
        signals.cme_basis_signal = _classify_cme_basis(basis)
        signals.cme_front_price = round(cme, 2)
        signals.spot_price_for_basis = round(spot_for_basis, 2)
        logger.info(f"[SMART-MONEY] CME Basis: {basis:+.3f}% (CME={cme}, spot={spot_for_basis})")

    # 4. Funding rate dispersion
    rates = {}
    for sym in DEFAULT_FUNDING_SYMBOLS:
        v = _v(f"funding_{sym}")
        if v is not None:
            rates[sym] = v
    signals.funding_rates = rates
    avg, sig, alignment = _classify_funding_dispersion(rates)
    signals.funding_avg_pct = round(avg, 4) if avg is not None else None
    signals.funding_dispersion_signal = sig
    signals.funding_alignment = alignment
    if rates:
        logger.info(f"[SMART-MONEY] Funding avg: {avg:+.4f}% across {len(rates)} pairs, alignment={alignment}")

    return signals


# ── Score contribution ────────────────────────────────────────────────────────


def smart_money_score_contribution(s: SmartMoneySignals) -> tuple[int, list[str], list[str]]:
    """Считает вклад smart-money блока в общий score.

    Returns: (score_delta, bullish_reasons, bearish_reasons)
    """
    score = 0
    bullish: list[str] = []
    bearish: list[str] = []

    # 1. Top-trader L/S
    if s.top_trader_ls_ratio is not None:
        if s.top_trader_ls_ratio >= 1.5:
            score += 2
            bullish.append(f"Top-trader L/S {s.top_trader_ls_ratio:.2f} — крупные в лонге")
        elif s.top_trader_ls_ratio >= 1.2:
            score += 1
            bullish.append(f"Top-trader L/S {s.top_trader_ls_ratio:.2f} — лёгкий лонг-перевес")
        elif s.top_trader_ls_ratio <= 0.7:
            score -= 2
            bearish.append(f"Top-trader L/S {s.top_trader_ls_ratio:.2f} — крупные в шорте")
        elif s.top_trader_ls_ratio <= 0.85:
            score -= 1
            bearish.append(f"Top-trader L/S {s.top_trader_ls_ratio:.2f} — лёгкий шорт-перевес")

    # 2. Coinbase Premium
    if s.coinbase_premium_pct is not None:
        if s.coinbase_premium_pct >= 0.20:
            score += 2
            bullish.append(f"Coinbase Premium +{s.coinbase_premium_pct:.2f}% — US-институционалы покупают")
        elif s.coinbase_premium_pct >= 0.05:
            score += 1
            bullish.append(f"Coinbase Premium +{s.coinbase_premium_pct:.2f}% — US bid pressure")
        elif s.coinbase_premium_pct <= -0.20:
            score -= 2
            bearish.append(f"Coinbase Premium {s.coinbase_premium_pct:.2f}% — US-институционалы продают")
        elif s.coinbase_premium_pct <= -0.05:
            score -= 1
            bearish.append(f"Coinbase Premium {s.coinbase_premium_pct:.2f}% — US sell pressure")

    # 3. CME Basis
    if s.cme_basis_pct is not None:
        if s.cme_basis_pct >= 0.30:
            score += 1
            bullish.append(f"CME Basis +{s.cme_basis_pct:.2f}% — институционалы платят за фьючерс")
        elif s.cme_basis_pct <= -0.30:
            score -= 1
            bearish.append(f"CME Basis {s.cme_basis_pct:.2f}% — backwardation")

    # 4. Funding dispersion
    if s.funding_avg_pct is not None and s.funding_alignment:
        if s.funding_alignment == "ALL_SHORT" and s.funding_avg_pct < -0.005:
            score += 2
            bullish.append(f"Funding {s.funding_avg_pct:+.4f}% — массовый шорт → риск squeeze (contrarian-бычий)")
        elif s.funding_alignment == "ALL_LONG" and s.funding_avg_pct > 0.05:
            score -= 2
            bearish.append(f"Funding {s.funding_avg_pct:+.4f}% — перегретый лонг → риск squeeze")
        elif s.funding_alignment == "ALL_LONG" and s.funding_avg_pct > 0.01:
            score += 1
            bullish.append(f"Funding {s.funding_avg_pct:+.4f}% — широкое лонг-настроение")

    return score, bullish, bearish


# ── Formatter ─────────────────────────────────────────────────────────────────


def format_smart_money_for_agents(s: SmartMoneySignals) -> str:
    """Форматирует smart-money блок для AI-агентов."""
    lines = ["🐋 SMART-MONEY СИГНАЛЫ (источник: Binance Futures / Coinbase / Yahoo CME):"]

    # Top trader
    if s.top_trader_ls_ratio is not None:
        lines.append(
            f"• Top-trader L/S Position Ratio: {s.top_trader_ls_ratio:.2f} — "
            f"{s.top_trader_ls_signal}"
        )
    else:
        lines.append("• Top-trader L/S Position Ratio: N/A (Binance Futures недоступен)")

    # Coinbase Premium
    if s.coinbase_premium_pct is not None:
        lines.append(
            f"• Coinbase Premium: {s.coinbase_premium_pct:+.3f}% "
            f"(CB ${s.coinbase_price_usd:,.0f} vs Binance ${s.binance_price_usdt:,.0f}) — "
            f"{s.coinbase_premium_signal}"
        )
    else:
        lines.append("• Coinbase Premium: N/A")

    # CME Basis
    if s.cme_basis_pct is not None:
        lines.append(
            f"• CME Basis: {s.cme_basis_pct:+.3f}% "
            f"(CME ${s.cme_front_price:,.0f} vs spot ${s.spot_price_for_basis:,.0f}) — "
            f"{s.cme_basis_signal}"
        )
    else:
        lines.append("• CME Basis: N/A")

    # Funding dispersion
    if s.funding_rates:
        rates_str = ", ".join(
            f"{sym.replace('USDT','')} {v*100:+.4f}%"
            for sym, v in s.funding_rates.items()
        )
        lines.append(f"• Funding: {rates_str}")
        lines.append(f"  → {s.funding_dispersion_signal}")
    else:
        lines.append("• Funding rates: N/A")

    lines.append("")
    lines.append(
        "💡 Smart-money интерпретация: ритейл-трейдеры в массе своей убыточны, "
        "поэтому позиции топ-трейдеров и приток через Coinbase важнее, "
        "чем глобальный long/short ratio. Подтверждение нескольких сигналов одновременно "
        "= высокая уверенность."
    )

    return "\n".join(lines)


# ── Compact formatter (для Telegram /markets и /daily) ───────────────────────


_SYMBOL_ICON = {
    "BTCUSDT": "₿",
    "ETHUSDT": "Ξ",
    "SOLUSDT": "◎",
    "BNBUSDT": "🅱",
    "XRPUSDT": "✕",
}


def _ls_tag(ratio: float) -> tuple[str, str]:
    """Возвращает (emoji, короткий ярлык) для L/S ratio."""
    if ratio >= 1.5:
        return "🟢", "лонгят сильно"
    if ratio >= 1.2:
        return "🟢", "лонгят"
    if ratio <= 0.7:
        return "🔴", "шортят сильно"
    if ratio <= 0.85:
        return "🔴", "шортят"
    return "⚪", "нейтрал"


def format_smart_money_compact(s: SmartMoneySignals) -> Optional[str]:
    """Короткий Telegram-friendly блок smart-money для /markets и /daily.

    Возвращает None если совсем нет данных (модуль молчит).
    """
    has_any = bool(
        s.top_trader_ls_per_symbol
        or s.coinbase_premium_pct is not None
        or s.cme_basis_pct is not None
        or s.funding_rates
    )
    if not has_any:
        return None

    lines = ["🏛 *SMART-MONEY (топ-трейдеры / институционалы)*"]

    if s.top_trader_ls_per_symbol:
        lines.append("")
        lines.append("📊 *Top-trader L/S по парам (Binance Futures):*")
        for sym in DEFAULT_LS_SYMBOLS:
            ratio = s.top_trader_ls_per_symbol.get(sym)
            name = sym.replace("USDT", "")
            icon = _SYMBOL_ICON.get(sym, "•")
            if ratio is None:
                lines.append(f"{icon} {name}: N/A")
                continue
            emoji, tag = _ls_tag(ratio)
            lines.append(f"{icon} {name}: `{ratio:.2f}` {emoji} {tag}")

    macro_lines: list[str] = []

    if s.coinbase_premium_pct is not None:
        prem = s.coinbase_premium_pct
        if prem >= 0.20:
            tag = "🟢 US-биды сильные"
        elif prem >= 0.05:
            tag = "🟢 US bid pressure"
        elif prem <= -0.20:
            tag = "🔴 US-продажи"
        elif prem <= -0.05:
            tag = "🔴 US sell pressure"
        else:
            tag = "⚪ нейтрал"
        macro_lines.append(f"🇺🇸 *Coinbase Premium:* `{prem:+.2f}%` {tag}")

    if s.cme_basis_pct is not None:
        basis = s.cme_basis_pct
        if basis >= 0.30:
            tag = "🟢 contango (бычий)"
        elif basis <= -0.30:
            tag = "🔴 backwardation (стресс)"
        else:
            tag = "⚪ нейтрал"
        macro_lines.append(f"📜 *CME Basis:* `{basis:+.2f}%` {tag}")

    if s.funding_avg_pct is not None and s.funding_alignment:
        avg = s.funding_avg_pct
        align = s.funding_alignment
        if align == "ALL_LONG" and avg > 0.05:
            tag = "⚠️ перегретый лонг — squeeze risk"
        elif align == "ALL_SHORT" and avg < -0.005:
            tag = "⚡ массовый шорт — contrarian-бычий"
        elif align == "ALL_LONG":
            tag = "🟢 лонг-настроение"
        elif align == "ALL_SHORT":
            tag = "🔴 шорт-настроение"
        else:
            tag = "⚪ смешанный"
        macro_lines.append(f"💸 *Funding avg:* `{avg:+.4f}%` [{align}] {tag}")

    if macro_lines:
        lines.append("")
        lines.extend(macro_lines)

    return "\n".join(lines)


# ── Test ──────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    async def _test():
        print("=== ТЕСТ SMART-MONEY ===\n")
        s = await fetch_smart_money_signals()
        print(format_smart_money_for_agents(s))
        print()
        delta, bull, bear = smart_money_score_contribution(s)
        print(f"Score contribution: {delta:+d}")
        print(f"Bullish reasons: {bull}")
        print(f"Bearish reasons: {bear}")

    asyncio.run(_test())
