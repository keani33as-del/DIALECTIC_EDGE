"""
aggregator.py — Сборщик всех данных для AI-анализа

Собирает:
1. On-chain метрики (CoinGecko)
2. Расширенные макро данные (FRED)
3. Скоринг и баллы
4. Цены (из основного потока)

И формирует единый контекст для AI агентов.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from .onchain import fetch_onchain_metrics, format_onchain_for_agents, OnChainMetrics
from .macro_extended import fetch_extended_macro, format_macro_extended_for_agents, MacroExtended
from .scorer import (
    calculate_market_score, 
    get_critical_signals,
    format_scored_context_for_agents,
    format_signal_block_for_debates,
    MarketScore
)

logger = logging.getLogger(__name__)


@dataclass
class EnrichedData:
    """Все собранные данные + скоринг"""
    onchain: OnChainMetrics = None
    macro: MacroExtended = None
    score: MarketScore = None
    
    def __post_init__(self):
        if self.onchain is None:
            self.onchain = OnChainMetrics()
        if self.macro is None:
            self.macro = MacroExtended()
        if self.score is None:
            self.score = MarketScore()


async def build_enriched_context(
    prices: dict = None,
    vix: Optional[float] = None,
    fear_greed: Optional[float] = None,
    sentiment_label: Optional[str] = None,
    trend_btc: Optional[str] = None,
    rsi_btc: Optional[float] = None,
    rsi_spy: Optional[float] = None,
) -> tuple[str, EnrichedData]:
    """
    Собирает все данные и формирует контекст для AI.
    
    Returns: (context_string, EnrichedData)
    """
    logger.info("[AGGREGATOR] Starting enriched context build...")
    enriched = EnrichedData()
    
    # Параллельно собираем данные
    onchain_task = fetch_onchain_metrics()
    macro_task = fetch_extended_macro()
    
    logger.info("[AGGREGATOR] Fetching on-chain + macro data in parallel...")
    onchain_data, macro_data = await asyncio.gather(onchain_task, macro_task)
    
    enriched.onchain = onchain_data
    enriched.macro = macro_data
    logger.info("[AGGREGATOR] On-chain + macro fetched OK")
    
    # Проверяем критические стоп-факторы
    critical_bearish, critical_bullish = get_critical_signals(
        mvrv=enriched.onchain.mvrv if enriched.onchain else None,
        vix=vix,
        fear_greed=fear_greed,
    )
    logger.info(f"[AGGREGATOR] Critical signals — bearish={critical_bearish}, bullish={critical_bullish}")
    
    # Вычисляем скор
    score = calculate_market_score(
        # Макро
        vix=vix,
        yield_curve_spread=enriched.macro.yield_spread if enriched.macro else None,
        qe_qt_mode=enriched.macro.qe_qt_mode if enriched.macro else None,
        credit_spread=enriched.macro.hy_spread if enriched.macro else None,
        
        # Ончейн
        mvrv=enriched.onchain.mvrv if enriched.onchain else None,
        sopr=enriched.onchain.sopr if enriched.onchain else None,
        exchange_reserves_trend="down" if enriched.onchain and getattr(enriched.onchain, "reserves_signal", None) and "HODL" in enriched.onchain.reserves_signal else "up",
        
        # Технические
        rsi=rsi_btc,
        trend=trend_btc,
        
        # Сентимент
        fear_greed=fear_greed,
        sentiment=sentiment_label,
    )
    
    # Добавляем стоп-факторы
    score.has_critical_bearish = critical_bearish
    score.has_critical_bullish = critical_bullish
    
    # Финальный вердикт
    if critical_bearish:
        score.final_verdict = "BEARISH"
    elif critical_bullish:
        score.final_verdict = "BULLISH"
    else:
        score.final_verdict = score.preliminary_verdict
    
    enriched.score = score
    
    logger.info(f"[AGGREGATOR] Scoring — total={score.total_score}, macro={score.macro_score}, onchain={score.onchain_score}, tech={score.technical_score}, sentiment={score.sentiment_score}")
    logger.info(f"[AGGREGATOR] Verdict — preliminary={score.preliminary_verdict}, final={score.final_verdict}")
    
    # Формируем контекст
    context_parts = []
    
    # 1. On-chain метрики
    onchain_str = format_onchain_for_agents(enriched.onchain)
    context_parts.append(onchain_str)
    
    # 2. Расширенные макро
    macro_str = format_macro_extended_for_agents(enriched.macro)
    context_parts.append("")
    context_parts.append(macro_str)
    
    # 3. Система баллов
    scored_str = format_scored_context_for_agents(enriched.score)
    context_parts.append("")
    context_parts.append(scored_str)
    
    # 4. СИГНАЛ БЛОК для Bull/Bear дебатов (НОВЫЙ!)
    signal_block = format_signal_block_for_debates(enriched.score, enriched.onchain, enriched.macro)
    context_parts.append("")
    context_parts.append(signal_block)
    
    # 5. Финальный вердикт для агентов
    context_parts.append("")
    verdict_emoji = "🟢" if score.final_verdict == "BULLISH" else "🔴" if score.final_verdict == "BEARISH" else "⚪"
    context_parts.append(f"{verdict_emoji} СИСТЕМА БАЛЛОВ РЕКОМЕНДУЕТ: **{score.final_verdict}**")
    
    if critical_bearish:
        context_parts.append("⚠️ СТОП-ФАКТОР: Критические условия указывают на медвежий рынок!")
    elif critical_bullish:
        context_parts.append("🔵 СТОП-ФАКТОР: Критические условия указывают на историческое дно!")
    
    logger.info(f"[AGGREGATOR] DONE — context length={len(context_parts)} parts, final_verdict={score.final_verdict}")
    return "\n".join(context_parts), enriched


def enrich_prices_with_scores(
    prices: dict,
    score: MarketScore,
    enriched: EnrichedData
) -> dict:
    """
    Обогащает словарь цен дополнительными метриками.
    Используется для передачи в шаблоны и отчёты.
    """
    enriched_prices = prices.copy()
    
    # Добавляем on-chain
    if enriched.onchain:
        enriched_prices["MVRV"] = enriched.onchain.mvrv
        enriched_prices["MVRV_SIGNAL"] = enriched.onchain.mvrv_signal
        enriched_prices["SOPR"] = enriched.onchain.sopr
        enriched_prices["SOPR_SIGNAL"] = enriched.onchain.sopr_signal
    
    # Добавляем макро
    if enriched.macro:
        enriched_prices["FED_BALANCE"] = enriched.macro.fed_balance_billions
        enriched_prices["FED_SIGNAL"] = enriched.macro.qe_qt_mode
        enriched_prices["YIELD_10Y"] = enriched.macro.yield_10y
        enriched_prices["YIELD_2Y"] = enriched.macro.yield_2y
        enriched_prices["YIELD_SPREAD"] = enriched.macro.yield_spread
        enriched_prices["CREDIT_SPREAD"] = enriched.macro.hy_spread
    
    # Добавляем скор
    if score:
        enriched_prices["MARKET_SCORE"] = score.total_score
        enriched_prices["MARKET_VERDICT"] = score.final_verdict
        enriched_prices["SCORE_MACRO"] = score.macro_score
        enriched_prices["SCORE_ONCHAIN"] = score.onchain_score
        enriched_prices["SCORE_TECHNICAL"] = score.technical_score
        enriched_prices["SCORE_SENTIMENT"] = score.sentiment_score
    
    return enriched_prices


# ─── Test ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    async def test():
        print("=== ТЕСТ АГРЕГАТОРА ===\n")
        
        # Пример данных из основного потока
        context, enriched = await build_enriched_context(
            vix=18.5,
            fear_greed=45,
            sentiment_label="NEUTRAL",
            trend_btc="UPTREND",
            rsi_btc=62,
        )
        
        print("=== СФОРМИРОВАННЫЙ КОНТЕКСТ ===")
        print(context)
        print()
        print("=== ENRICHED DATA ===")
        print(f"MVRV: {enriched.onchain.mvrv:.2f}")
        print(f"SOPR: {enriched.onchain.sopr:.3f}")
        print(f"Fed Balance: ${enriched.macro.fed_balance_billions:.0f}B")
        print(f"QE/QT: {enriched.macro.qe_qt_mode}")
        print(f"Yield Curve: {enriched.macro.yield_spread:.2f}%")
        print()
        print(f"FINAL VERDICT: {enriched.score.final_verdict}")
        print(f"Total Score: {enriched.score.total_score:+d}")
    
    asyncio.run(test())
