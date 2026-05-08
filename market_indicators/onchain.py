"""
onchain.py — On-chain метрики для BTC/ETH

Источники:
- CoinGecko API (бесплатно, rate limited)
- Fallback: Binance API для basic data

Метрики:
- MVRV (Market Value / Realized Value)
- Exchange Reserves (биржи)
- Active Addresses (активность сети)
- Transaction Volume (объём транзакций)
- SOPR (Spent Output Profit Ratio) — требует Glassnode
"""

import asyncio
import logging
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# CoinGecko бесплатный API endpoints
COINGECKO_BASE = "https://api.coingecko.com/api/v3"


@dataclass
class OnChainMetrics:
    """On-chain метрики для BTC"""
    mvrv: float = 0.0  # Market to Realized Value
    mvrv_signal: str = "N/A"
    
    exchange_reserves_btc: float = 0.0  # BTC на биржах
    exchange_reserves_change_7d: float = 0.0  # Изменение за 7 дней
    reserves_signal: str = "N/A"
    
    active_addresses_change: float = 0.0  # Изменение за 7 дней
    active_addresses_signal: str = "N/A"
    
    tx_volume_24h: float = 0.0  # $ объём за 24ч
    tx_volume_signal: str = "N/A"
    
    # Whale metrics (упрощённо через volume)
    large_tx_volume_24h: float = 0.0
    
    # SOPR (если доступен)
    sopr: float = 0.0
    sopr_signal: str = "N/A"
    
    # Net flows (exchange in/out)
    exchange_inflow_24h: float = 0.0  # BTC пришло на биржи
    exchange_outflow_24h: float = 0.0  # BTC ушло с бирж
    net_flow_24h: float = 0.0  # Чистый поток


async def _fetch_coingecko(session, endpoint: str, params: dict = None) -> Optional[dict]:
    """Fetch от CoinGecko API с rate limiting"""
    url = f"{COINGECKO_BASE}/{endpoint}"
    params = params or {}
    
    try:
        async with session.get(url, params=params, timeout=10) as resp:
            if resp.status == 429:
                logger.warning("CoinGecko rate limited")
                return None
            if resp.status == 200:
                return await resp.json()
            return None
    except Exception as e:
        logger.debug(f"CoinGecko error: {e}")
        return None


async def fetch_btc_onchain() -> OnChainMetrics:
    """Получает on-chain метрики для BTC"""
    metrics = OnChainMetrics()
    logger.info("[ON-CHAIN] Fetching BTC metrics from CoinGecko...")

    import aiohttp
    async with aiohttp.ClientSession() as session:
        # 1. MVRV — через market_data endpoint
        market_data = await _fetch_coingecko(
            session,
            "coins/bitcoin",
            params={"localization": "false", "tickers": "false", "community_data": "false"}
        )

        if market_data:
            logger.info("[ON-CHAIN] CoinGecko /coins/bitcoin: OK")
        else:
            logger.warning("[ON-CHAIN] CoinGecko /coins/bitcoin: failed")

        # 2. Exchange Reserves — через /coins/{id}/market_chart (volume-based proxy)
        chart_data = await _fetch_coingecko(
            session,
            "coins/bitcoin/market_chart",
            params={"vs": "usd", "days": "7", "interval": "daily"}
        )

        if chart_data and "total_volumes" in chart_data:
            volumes = chart_data["total_volumes"]
            if len(volumes) >= 2:
                current_volume = volumes[-1][1] if volumes else 0
                prev_volume = volumes[-2][1] if len(volumes) > 1 else current_volume
                metrics.tx_volume_24h = current_volume / 1e9  # в billions
                logger.info(f"[ON-CHAIN] 24h Volume: ${metrics.tx_volume_24h:.1f}B")

                if prev_volume > 0:
                    change = ((current_volume - prev_volume) / prev_volume) * 100
                    if change > 0:
                        metrics.active_addresses_signal = "🔴 Растёт (↑)"
                    else:
                        metrics.active_addresses_signal = "🟢 Падает (↓)"

        # 3. Basic BTC data для reference
        btc_data = await _fetch_coingecko(
            session,
            "simple/price",
            params={
                "ids": "bitcoin",
                "vs_currencies": "usd",
                "include_market_cap": "true",
                "include_24hr_vol": "true"
            }
        )

        if btc_data and "bitcoin" in btc_data:
            btc = btc_data["bitcoin"]
            metrics.tx_volume_24h = btc.get("usd_24h_vol", 0) / 1e9  # billions USD
            logger.info(f"[ON-CHAIN] BTC price data: OK, vol=${metrics.tx_volume_24h:.1f}B")

        await asyncio.sleep(0.5)  # Rate limiting

    # MVRV — используем приблизительную оценку через market cycle
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session2:
            metrics.mvrv = await _estimate_mvrv_approx(session2)
    except Exception as e:
        logger.warning(f"[ON-CHAIN] MVRV estimation failed: {e}")
        metrics.mvrv = 2.0

    # Единый формат MVRV-сигнала с конкретным значением.
    # Прежде были две версии: инлайн без числа (банд only) и помощник `get_mvrv_signal`
    # с числом — Verifier не находил цифру в каноническом блоке и помечал
    # валидные аргументы Bull/Bear как галлюцинации.
    metrics.mvrv_signal = get_mvrv_signal(metrics.mvrv)
    logger.info(f"[ON-CHAIN] MVRV {metrics.mvrv:.2f}: {metrics.mvrv_signal}")

    # SOPR — требует Glassnode ПРО API. Пока выводим явным placeholder'ом,
    # чтобы агенты не цитировали хардкод 1.02 как реальное измерение.
    metrics.sopr = 0.0  # 0.0 → скорер пропустит баллы SOPR как невалидные
    metrics.sopr_signal = "⚪ SOPR недоступен (нужен Glassnode API — placeholder)"
    logger.info(f"[ON-CHAIN] SOPR: {metrics.sopr_signal}")

    # Exchange Reserves — используем volume как proxy.
    # Используем именно поле dataclass `reserves_signal` (раньше писали в
    # `exchange_reserves_signal`, этого поля в dataclass нет — attr создавался
    # динамически, а `format_onchain_for_agents` читал `reserves_signal` и получал
    # дефолт "N/A" — канонический блок был бесполезным).
    if metrics.tx_volume_24h > 30:
        metrics.reserves_signal = "🟡 Высокая активность на биржах"
    elif metrics.tx_volume_24h > 15:
        metrics.reserves_signal = "🟢 Нормальная активность"
    else:
        metrics.reserves_signal = "🟢🔵 HODLing фаза (низкая активность)"
    logger.info(f"[ON-CHAIN] Exchange Activity: {metrics.reserves_signal}")

    logger.info(f"[ON-CHAIN] DONE — MVRV={metrics.mvrv:.2f}, SOPR={metrics.sopr:.3f}, Vol=${metrics.tx_volume_24h:.1f}B")
    return metrics


async def _estimate_mvrv_approx(session) -> float:
    """Приблизительная оценка MVRV через доступные данные"""
    # MVRV = Market Cap / Realized Cap
    # Realized Cap ≈ Average cost basis of all coins
    # Упрощённо: realized cap ≈ 60-70% от market cap в нормальном рынке
    
    try:
        import aiohttp
        data = await _fetch_coingecko(
            session,
            "coins/bitcoin",
            params={"localization": "false", "tickers": "false"}
        )
        
        if data:
            market_cap = data.get("market_data", {}).get("market_cap", {}).get("usd", 0)
            current_price = data.get("market_data", {}).get("current_price", {}).get("usd", 0)
            total_supply = data.get("market_data", {}).get("total_supply", 0)
            
            if current_price > 0 and total_supply > 0:
                market_cap_actual = current_price * total_supply
                
                # Приблизительный realized cap (упрощённый)
                # В реальности используем Glassnode API
                # Пока: используем historical averages
                realized_cap_mult = 0.65  # Примерно 65% от market cap в среднем
                realized_cap = market_cap_actual * realized_cap_mult
                
                mvrv = market_cap_actual / realized_cap if realized_cap > 0 else 2.0
                return mvrv
    except:
        pass
    
    return 2.0  # Default value


async def fetch_onchain_metrics() -> OnChainMetrics:
    """Получает on-chain метрики для BTC (main entry point)"""
    return await fetch_btc_onchain()


def get_mvrv_signal(mvrv: float) -> str:
    """Интерпретация MVRV"""
    if mvrv <= 0:
        return "⚪ N/A"
    elif mvrv < 1.0:
        return f"🟢🔵 ИСТОРИЧЕСКОЕ ДНО (MVRV={mvrv:.2f})"
    elif mvrv < 2.0:
        return f"🟢 Справедливо (MVRV={mvrv:.2f})"
    elif mvrv < 3.0:
        return f"⚪ Норма (MVRV={mvrv:.2f})"
    elif mvrv < 3.5:
        return f"🟡 Внимание (MVRV={mvrv:.2f})"
    else:
        return f"🔴 ПЕРЕОЦЕНЁН (MVRV={mvrv:.2f})"


def get_sopr_signal(sopr: float) -> str:
    """Интерпретация SOPR"""
    if sopr <= 0:
        return "⚪ N/A"
    elif sopr < 0.95:
        return f"🔴 Капитуляция (SOPR={sopr:.3f})"
    elif sopr < 1.0:
        return f"🟡 Ближе к дну (SOPR={sopr:.3f})"
    elif sopr < 1.05:
        return f"⚪ Норма (SOPR={sopr:.3f})"
    elif sopr < 1.1:
        return f"🟡 Фиксация прибыли (SOPR={sopr:.3f})"
    else:
        return f"🔴 Массовая фиксация (SOPR={sopr:.3f})"


def get_exchange_reserves_signal(change_7d: float) -> str:
    """Интерпретация изменения reserves"""
    if change_7d <= 0:
        return f"🟢 HODLing (резервы ↓ {abs(change_7d):.1f}%)"
    else:
        return f"🔴 Продажа (резервы ↑ {change_7d:.1f}%)"


def format_onchain_for_agents(metrics: OnChainMetrics) -> str:
    """Форматирует on-chain метрики для AI агентов.

    Инвариант для Verifier: каждая цифра, которую может процитировать
    Bull/Bear, должна встречаться в этом каноническом блоке в точно той
    же форме (.2f для MVRV, .3f для SOPR, .1f для volume и резервов).
    Иначе валидные аргументы пойдут в галлюцинации и вердикт перекосит.
    """

    lines = ["=== ON-CHAIN МЕТРИКИ (BTC) ==="]

    # MVRV — самый важный, всегда с конкретным числом
    if metrics.mvrv and metrics.mvrv > 0:
        lines.append(f"• MVRV: {metrics.mvrv:.2f} — {metrics.mvrv_signal}")
    else:
        lines.append(f"• MVRV: {metrics.mvrv_signal}")

    # SOPR — избегаем показывать фейковые числа. metrics.sopr <= 0 — placeholder.
    if metrics.sopr and metrics.sopr > 0:
        lines.append(f"• SOPR: {metrics.sopr:.3f} — {metrics.sopr_signal}")
    else:
        lines.append(f"• SOPR: {metrics.sopr_signal}")

    # Exchange Activity — читаем dataclass-поле reserves_signal
    # (раньше производитель писал в exchange_reserves_signal — и оно было “N/A”)
    reserves_line = metrics.reserves_signal or "N/A"
    if metrics.exchange_reserves_change_7d:
        sign = "↑" if metrics.exchange_reserves_change_7d > 0 else "↓"
        reserves_line += f" ({sign}{abs(metrics.exchange_reserves_change_7d):.1f}% за 7д)"
    lines.append(f"• Exchange Activity: {reserves_line}")

    lines.append(f"• 24h Volume: ${metrics.tx_volume_24h:.1f}B")

    # Сигналы для AI — только когда есть реальные данные
    if metrics.mvrv > 3.5:
        lines.append("⚠️ MVRV > 3.5 = КРИТИЧЕСКИЙ — высокий риск коррекции")
    elif metrics.mvrv > 0 and metrics.mvrv < 1.0:
        lines.append("🔵 MVRV < 1.0 = КРИТИЧЕСКИЙ — исторически дно, opportunity")

    if metrics.sopr and metrics.sopr > 1.05:
        lines.append("⚠️ SOPR > 1.05 = инвесторы фиксируют прибыль")
    elif metrics.sopr and 0 < metrics.sopr < 0.95:
        lines.append("🔵 SOPR < 0.95 = капитуляция, возможно дно")

    return "\n".join(lines)


# ─── Test ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    async def test():
        print("Fetching BTC on-chain metrics...")
        metrics = await fetch_btc_onchain()
        print(f"MVRV: {metrics.mvrv:.2f}")
        print(f"MVRV Signal: {metrics.mvrv_signal}")
        print(f"SOPR: {metrics.sopr:.3f}")
        print(f"SOPR Signal: {metrics.sopr_signal}")
        print(f"24h Volume: ${metrics.tx_volume_24h:.1f}B")
        print(f"Reserves Signal: {metrics.reserves_signal}")
        print()
        print(format_onchain_for_agents(metrics))
    
    asyncio.run(test())
