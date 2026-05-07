"""
macro_extended.py — Расширенные макро данные

Что добавляем:
- Fed Balance Sheet (WALCL)
- Treasury Yields (DGS10, DGS2)
- Yield Curve Spread (DGS10 - DGS2)
- Credit Spreads (HYA for high yield)

Источник: FRED (Federal Reserve Economic Data) — бесплатно!
API Key: https://fred.stlouisfed.org/docs/api/api_key.html
"""

import asyncio
import logging
from typing import Optional
from dataclasses import dataclass

from config import FRED_API_KEY

logger = logging.getLogger(__name__)

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"


@dataclass
class MacroExtended:
    """Расширенные макро метрики"""
    
    # Fed Balance Sheet
    fed_balance_billions: float = 0.0  # $B
    fed_balance_change_1w: float = 0.0  # Изменение за неделю ($B)
    fed_balance_change_1m: float = 0.0  # Изменение за месяц ($B)
    fed_balance_signal: str = "N/A"
    
    # Treasury Yields
    yield_10y: float = 0.0  # 10-Year Treasury Yield
    yield_2y: float = 0.0   # 2-Year Treasury Yield
    yield_spread: float = 0.0  # 10Y - 2Y Spread
    yield_curve_signal: str = "N/A"
    
    # Credit Spreads
    hy_spread: float = 0.0  # High Yield Spread (HYA - 10Y)
    credit_signal: str = "N/A"
    
    # Режим QE/QT
    qe_qt_mode: str = "UNKNOWN"  # QE / QT / NEUTRAL


async def _fetch_fred_series(series_id: str, limit: int = 2) -> Optional[list]:
    """Fetch данные из FRED API"""
    if not FRED_API_KEY or FRED_API_KEY in ("", "твой_ключ", "YOUR_KEY"):
        return None
    
    try:
        import aiohttp
        url = FRED_BASE
        params = {
            "series_id": series_id,
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "limit": limit,
            "sort_order": "desc"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("observations", [])
                elif resp.status == 429:
                    logger.warning("FRED rate limited")
                return None
    except Exception as e:
        logger.debug(f"FRED {series_id}: {e}")
        return None


async def fetch_extended_macro() -> MacroExtended:
    """Получает расширенные макро данные"""
    macro = MacroExtended()
    logger.info("[MACRO] Fetching extended macro from FRED...")

    # Параллельные запросы к FRED
    balance_task = _fetch_fred_series("WALCL", limit=5)  # Fed Balance Sheet
    yield_10y_task = _fetch_fred_series("DGS10", limit=2)  # 10Y Yield
    yield_2y_task = _fetch_fred_series("DGS2", limit=2)   # 2Y Yield
    hy_spread_task = _fetch_fred_series("HYCD", limit=2)  # High Yield Spread

    balance_data, yield_10y_data, yield_2y_data, hy_data = await asyncio.gather(
        balance_task, yield_10y_task, yield_2y_task, hy_spread_task
    )

    logger.info(f"[MACRO] FRED responses — Balance: {bool(balance_data)}, 10Y: {bool(yield_10y_data)}, 2Y: {bool(yield_2y_data)}, HY: {bool(hy_data)}")

    # 1. Fed Balance Sheet
    if balance_data and len(balance_data) >= 1:
        current_val = balance_data[0].get("value")
        if current_val and current_val != ".":
            macro.fed_balance_billions = float(current_val) / 1000  # Convert to billions
            
            if len(balance_data) >= 2:
                prev_val = balance_data[1].get("value")
                if prev_val and prev_val != ".":
                    macro.fed_balance_change_1w = (float(current_val) - float(prev_val)) / 1000
            
            if len(balance_data) >= 5:
                month_val = balance_data[4].get("value")
                if month_val and month_val != ".":
                    macro.fed_balance_change_1m = (float(current_val) - float(month_val)) / 1000
            
            # Сигнал
            if macro.fed_balance_change_1w > 10:
                macro.qe_qt_mode = "QE"  # Printing
                macro.fed_balance_signal = f"🔵 QE (баланс растёт +${macro.fed_balance_change_1w:.0f}B за неделю)"
                logger.info(f"[MACRO] Fed Balance: QE mode, +${macro.fed_balance_change_1w:.0f}B/wk")
            elif macro.fed_balance_change_1w < -10:
                macro.qe_qt_mode = "QT"  # Tightening
                macro.fed_balance_signal = f"🔴 QT (баланс сокращается -${abs(macro.fed_balance_change_1w):.0f}B за неделю)"
                logger.warning(f"[MACRO] Fed Balance: QT mode, -${abs(macro.fed_balance_change_1w):.0f}B/wk")
            else:
                macro.qe_qt_mode = "NEUTRAL"
                macro.fed_balance_signal = f"⚪ NEUTRAL (баланс стабилен ${macro.fed_balance_billions:.0f}B)"
                logger.info(f"[MACRO] Fed Balance: NEUTRAL, ${macro.fed_balance_billions:.0f}B")
        else:
            logger.warning("[MACRO] Fed Balance: no data")
    
    # 2. Treasury Yields
    if yield_10y_data and len(yield_10y_data) >= 1:
        val = yield_10y_data[0].get("value")
        if val and val != ".":
            macro.yield_10y = float(val)
            logger.info(f"[MACRO] 10Y Yield: {macro.yield_10y:.2f}%")
    
    if yield_2y_data and len(yield_2y_data) >= 1:
        val = yield_2y_data[0].get("value")
        if val and val != ".":
            macro.yield_2y = float(val)
            logger.info(f"[MACRO] 2Y Yield: {macro.yield_2y:.2f}%")
    
    # Yield Curve Spread
    if macro.yield_10y > 0 and macro.yield_2y > 0:
        macro.yield_spread = macro.yield_10y - macro.yield_2y
        
        # Сигнал кривой
        if macro.yield_spread < -0.5:
            macro.yield_curve_signal = f"🔴 ИНВЕРТИРОВАНА (спред {macro.yield_spread:.2f}%) — рецессия вероятна"
            logger.warning(f"[MACRO] Yield Curve: ИНВЕРТИРОВАНА {macro.yield_spread:.2f}%")
        elif macro.yield_spread < 0:
            macro.yield_curve_signal = f"🟡 ЧАСТИЧНО ИНВЕРТИРОВАНА ({macro.yield_spread:.2f}%) — внимание"
            logger.warning(f"[MACRO] Yield Curve: частично инвертирована {macro.yield_spread:.2f}%")
        elif macro.yield_spread < 1.0:
            macro.yield_curve_signal = f"🟢 НОРМАЛЬНАЯ ({macro.yield_spread:.2f}%)"
            logger.info(f"[MACRO] Yield Curve: нормальная {macro.yield_spread:.2f}%")
        else:
            macro.yield_curve_signal = f"🟢 КРУТАЯ ({macro.yield_spread:.2f}%)"
            logger.info(f"[MACRO] Yield Curve: крутая {macro.yield_spread:.2f}%")
    else:
        logger.warning("[MACRO] Yield data unavailable")
    
    # 3. Credit Spreads
    if hy_data and len(hy_data) >= 1:
        val = hy_data[0].get("value")
        if val and val != ".":
            macro.hy_spread = float(val)
            
            if macro.hy_spread > 5.0:
                macro.credit_signal = f"🔴 СТРЕСС ({macro.hy_spread:.1f}%) — высокий риск"
                logger.warning(f"[MACRO] Credit Spread: СТРЕСС {macro.hy_spread:.1f}%")
            elif macro.hy_spread > 3.5:
                macro.credit_signal = f"🟡 ПОВЫШЕННЫЙ ({macro.hy_spread:.1f}%)"
                logger.info(f"[MACRO] Credit Spread: повышенный {macro.hy_spread:.1f}%")
            else:
                macro.credit_signal = f"🟢 НОРМАЛЬНО ({macro.hy_spread:.1f}%)"
                logger.info(f"[MACRO] Credit Spread: нормальный {macro.hy_spread:.1f}%")
    
    logger.info(f"[MACRO] DONE — Fed: {macro.fed_balance_signal}, Yield: {macro.yield_curve_signal}, Credit: {macro.credit_signal}")
    return macro


def get_yield_curve_signal(spread: float) -> str:
    """Интерпретация yield curve spread"""
    if spread < -0.5:
        return f"🔴 ИНВЕРТИРОВАНА ({spread:.2f}%) — РЕЦЕССИЯ РИСК"
    elif spread < 0:
        return f"🟡 ЧАСТИЧНО ИНВЕРТИРОВАНА ({spread:.2f}%)"
    elif spread < 1.0:
        return f"🟢 НОРМАЛЬНАЯ ({spread:.2f}%)"
    else:
        return f"🟢 КРУТАЯ ({spread:.2f}%)"


def get_fed_balance_signal(change_1w: float) -> str:
    """Интерпретация изменения баланса ФРС"""
    if change_1w > 10:
        return f"🔵 QE (+$>{change_1w:.0f}B) — ликвидность растёт"
    elif change_1w < -10:
        return f"🔴 QT (-${abs(change_1w):.0f}B) — ликвидность падает"
    else:
        return "⚪ Стабильно"


def format_macro_extended_for_agents(macro: MacroExtended) -> str:
    """Форматирует расширенные макро данные для AI агентов"""
    
    lines = ["=== МАКРО РАСШИРЕННОЕ (FRED) ==="]
    
    # Fed Balance
    lines.append(f"• Fed Balance: {macro.fed_balance_signal}")
    lines.append(f"• Режим: {macro.qe_qt_mode}")
    
    # Yields
    if macro.yield_10y > 0:
        lines.append(f"• 10Y Yield: {macro.yield_10y:.2f}%")
    if macro.yield_2y > 0:
        lines.append(f"• 2Y Yield: {macro.yield_2y:.2f}%")
    if macro.yield_spread != 0:
        lines.append(f"• Yield Curve: {macro.yield_curve_signal}")
    
    # Credit
    if macro.hy_spread > 0:
        lines.append(f"• Credit Spread (HYA): {macro.credit_signal}")
    
    # Критические сигналы
    if macro.qe_qt_mode == "QT":
        lines.append("⚠️ QT = ликвидность падает = МЕДВЕЖИЙ для рисковых активов")
    elif macro.qe_qt_mode == "QE":
        lines.append("🔵 QE = ликвидность растёт = БЫЧИЙ для рисковых активов")
    
    if macro.yield_spread < 0:
        lines.append("⚠️ Инвертированная кривая = рецессия риск")
    
    if macro.hy_spread > 5.0:
        lines.append("⚠️ Высокий credit spread = стресс на рынке")
    
    return "\n".join(lines)


# ─── Test ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    async def test():
        print("Fetching extended macro data from FRED...")
        macro = await fetch_extended_macro()
        
        print(f"Fed Balance: ${macro.fed_balance_billions:.0f}B")
        print(f"Fed Signal: {macro.fed_balance_signal}")
        print(f"QE/QT Mode: {macro.qe_qt_mode}")
        print()
        print(f"10Y Yield: {macro.yield_10y:.2f}%")
        print(f"2Y Yield: {macro.yield_2y:.2f}%")
        print(f"Spread: {macro.yield_spread:.2f}%")
        print(f"Yield Curve Signal: {macro.yield_curve_signal}")
        print()
        print(f"HY Spread: {macro.hy_spread:.1f}%")
        print(f"Credit Signal: {macro.credit_signal}")
        print()
        print(format_macro_extended_for_agents(macro))
    
    asyncio.run(test())
