"""
scorer.py — Система баллов для вердикта

Каждый индикатор получает баллы:
- Положительные = бычий
- Отрицательные = медвежий

Итоговый вердикт:
- Score > +3 → БЫЧИЙ
- Score < -3 → МЕДВЕЖИЙ
- Score -3..+3 → НЕЙТРАЛЬНЫЙ

Стоп-факторы (автоматический вердикт независимо от скора):
- MVRV > 3.5 → МЕДВЕЖИЙ
- VIX > 40 → МЕДВЕЖИЙ (кризис)
- VIX < 15 + F&G > 70 → БЫЧИЙ
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .onchain import OnChainMetrics
    from .macro_extended import MacroExtended

logger = logging.getLogger(__name__)


@dataclass
class MarketScore:
    """Результат скоринга рынка"""
    
    # Общий балл
    total_score: int = 0
    
    # По категориям
    macro_score: int = 0
    onchain_score: int = 0
    technical_score: int = 0
    sentiment_score: int = 0
    
    # Стоп-факторы
    has_critical_bearish: bool = False  # MVRV > 3.5, VIX > 40, etc.
    has_critical_bullish: bool = False  # MVRV < 1.0, F&G < 25
    
    # Детали
    bullish_signals: list = None
    bearish_signals: list = None
    
    # Итоговый вердикт (из скоринга, до стоп-факторов)
    preliminary_verdict: str = "NEUTRAL"
    
    # Финальный вердикт (после учёта стоп-факторов)
    final_verdict: str = "NEUTRAL"
    
    def __post_init__(self):
        if self.bullish_signals is None:
            self.bullish_signals = []
        if self.bearish_signals is None:
            self.bearish_signals = []
    
    @property
    def score_explanation(self) -> str:
        """Объяснение скора для AI"""
        parts = []
        
        if self.total_score > 0:
            parts.append(f"Скор: +{self.total_score} (бычий)")
        elif self.total_score < 0:
            parts.append(f"Скор: {self.total_score} (медвежий)")
        else:
            parts.append("Скор: 0 (нейтральный)")
        
        if self.macro_score != 0:
            parts.append(f"Макро: {self.macro_score:+d}")
        if self.onchain_score != 0:
            parts.append(f"Ончейн: {self.onchain_score:+d}")
        if self.technical_score != 0:
            parts.append(f"Техника: {self.technical_score:+d}")
        if self.sentiment_score != 0:
            parts.append(f"Сентимент: {self.sentiment_score:+d}")
        
        if self.has_critical_bearish:
            parts.append("⚠️ КРИТИЧЕСКИЙ МЕДВЕЖИЙ СТОП-ФАКТОР")
        elif self.has_critical_bullish:
            parts.append("🔵 КРИТИЧЕСКИЙ БЫЧИЙ СТОП-ФАКТОР")
        
        return " | ".join(parts)


def get_critical_signals(
    mvrv: Optional[float] = None,
    vix: Optional[float] = None,
    fear_greed: Optional[float] = None,
) -> tuple[bool, bool]:
    """
    Проверяет критические стоп-факторы.
    Returns: (has_critical_bearish, has_critical_bullish)
    """
    critical_bearish = False
    critical_bullish = False
    
    # MVRV > 3.5 = АВТОМАТИЧЕСКИ МЕДВЕЖИЙ
    if mvrv and mvrv > 3.5:
        critical_bearish = True
        logger.warning(f"MVRV {mvrv} > 3.5: CRITICAL BEARISH STOP")
    
    # VIX > 40 = КРИЗИС = АВТОМАТИЧЕСКИ МЕДВЕЖИЙ
    if vix and vix > 40:
        critical_bearish = True
        logger.warning(f"VIX {vix} > 40: CRISIS MODE")
    
    # VIX < 15 + Fear & Greed > 70 = ЭЙФОРИЯ = АВТОМАТИЧЕСКИ МЕДВЕЖИЙ (пузырь)
    if vix and vix < 15 and fear_greed and fear_greed > 70:
        critical_bearish = True
        logger.warning(f"VIX {vix} < 15 + F&G {fear_greed} > 70: BUBBLE WARNING")
    
    # MVRV < 1.0 = ИСТОРИЧЕСКОЕ ДНО = АВТОМАТИЧЕСКИ БЫЧИЙ
    if mvrv and mvrv < 1.0:
        critical_bullish = True
        logger.info(f"MVRV {mvrv} < 1.0: HISTORICAL BOTTOM — BUY SIGNAL")
    
    # Fear & Greed < 25 = ЭКСТРЕМАЛЬНЫЙ СТРАХ = АВТОМАТИЧЕСКИ БЫЧИЙ (противоположный)
    if fear_greed and fear_greed < 25:
        critical_bullish = True
        logger.info(f"Fear & Greed {fear_greed} < 25: EXTREME FEAR — BUY SIGNAL")
    
    return critical_bearish, critical_bullish


def calculate_market_score(
    # Макро
    vix: Optional[float] = None,
    fed_rate_change: Optional[str] = None,  # "up" / "down" / "stable"
    yield_curve_spread: Optional[float] = None,  # 10Y - 2Y
    qe_qt_mode: Optional[str] = None,  # "QE" / "QT" / "NEUTRAL"
    credit_spread: Optional[float] = None,
    
    # Ончейн
    mvrv: Optional[float] = None,
    sopr: Optional[float] = None,
    exchange_reserves_trend: Optional[str] = None,  # "up" / "down"
    whale_pressure: Optional[float] = None,  # 0-100 (buy pressure %)
    
    # Технические
    rsi: Optional[float] = None,
    price_vs_ma50: Optional[str] = None,  # "above" / "below"
    price_vs_ma200: Optional[str] = None,
    trend: Optional[str] = None,  # "UPTREND" / "DOWNTREND"
    
    # Сентимент
    fear_greed: Optional[float] = None,
    sentiment: Optional[str] = None,  # "BULLISH" / "BEARISH" / "NEUTRAL"
    
) -> MarketScore:
    """Вычисляет общий балл рынка"""
    
    score = MarketScore()
    bullish = score.bullish_signals
    bearish = score.bearish_signals
    
    # ══════════════════════════════════════════════
    # МАКРО (+30% вес в итоге, но считаем в баллах)
    # ══════════════════════════════════════════════
    
    # VIX
    if vix is not None:
        if vix < 15:
            score.macro_score += 2
            score.total_score += 2
            bullish.append(f"VIX {vix} < 15: RISK-ON")
        elif vix < 20:
            score.macro_score += 1
            score.total_score += 1
            bullish.append(f"VIX {vix} < 20: комфортный risk-on")
        elif vix > 30:
            score.macro_score -= 2
            score.total_score -= 2
            bearish.append(f"VIX {vix} > 30: высокая волатильность")
        elif vix > 25:
            score.macro_score -= 1
            score.total_score -= 1
            bearish.append(f"VIX {vix} > 25: risk-off")
    
    # Fed Rate
    if fed_rate_change == "down":
        score.macro_score += 2
        score.total_score += 2
        bullish.append("Fed ставка снижается")
    elif fed_rate_change == "up":
        score.macro_score -= 2
        score.total_score -= 2
        bearish.append("Fed ставка растёт")
    
    # Yield Curve
    if yield_curve_spread is not None:
        if yield_curve_spread < -0.5:
            score.macro_score -= 2
            score.total_score -= 2
            bearish.append(f"Кривая ИНВЕРТИРОВАНА ({yield_curve_spread:.1f}%): рецессия риск")
        elif yield_curve_spread < 0:
            score.macro_score -= 1
            score.total_score -= 1
            bearish.append(f"Кривая частично инвертирована ({yield_curve_spread:.1f}%)")
        elif yield_curve_spread > 1.0:
            score.macro_score += 1
            score.total_score += 1
            bullish.append(f"Кривая крутая ({yield_curve_spread:.1f}%): экономика сильна")
    
    # QE/QT
    if qe_qt_mode == "QE":
        score.macro_score += 2
        score.total_score += 2
        bullish.append("Fed QE: ликвидность растёт")
    elif qe_qt_mode == "QT":
        score.macro_score -= 2
        score.total_score -= 2
        bearish.append("Fed QT: ликвидность падает")
    
    # Credit Spread
    if credit_spread is not None:
        if credit_spread > 5.0:
            score.macro_score -= 2
            score.total_score -= 2
            bearish.append(f"Credit spread высокий ({credit_spread:.1f}%): стресс")
        elif credit_spread > 3.5:
            score.macro_score -= 1
            score.total_score -= 1
            bearish.append(f"Credit spread повышен ({credit_spread:.1f}%)")
    
    # ══════════════════════════════════════════════
    # ОНЧАЙН (+30% вес)
    # ══════════════════════════════════════════════
    
    # MVRV
    if mvrv is not None:
        if mvrv < 1.0:
            score.onchain_score += 3  # Сильный сигнал
            score.total_score += 3
            bullish.append(f"MVRV {mvrv:.2f} < 1.0: ИСТОРИЧЕСКОЕ ДНО")
        elif mvrv < 2.0:
            score.onchain_score += 1
            score.total_score += 1
            bullish.append(f"MVRV {mvrv:.2f}: справедливая цена")
        elif mvrv > 3.0:
            score.onchain_score -= 2
            score.total_score -= 2
            bearish.append(f"MVRV {mvrv:.2f} > 3.0: переоценка")
        elif mvrv > 3.5:
            score.onchain_score -= 3
            score.total_score -= 3
            bearish.append(f"MVRV {mvrv:.2f} > 3.5: ПЕРЕОЦЕНЁН, пузырь")
    
    # SOPR
    if sopr is not None:
        if sopr < 0.95:
            score.onchain_score += 1
            score.total_score += 1
            bullish.append(f"SOPR {sopr:.3f} < 0.95: капитуляция")
        elif sopr > 1.05:
            score.onchain_score -= 1
            score.total_score -= 1
            bearish.append(f"SOPR {sopr:.3f} > 1.05: фиксация прибыли")
    
    # Exchange Reserves
    if exchange_reserves_trend == "down":
        score.onchain_score += 1
        score.total_score += 1
        bullish.append("Reserves падают: HODLing")
    elif exchange_reserves_trend == "up":
        score.onchain_score -= 1
        score.total_score -= 1
        bearish.append("Reserves растут: продажа")
    
    # Whale Activity
    if whale_pressure is not None:
        if whale_pressure > 70:
            score.onchain_score += 2
            score.total_score += 2
            bullish.append(f"Whale buy pressure {whale_pressure:.0f}%")
        elif whale_pressure < 30:
            score.onchain_score -= 2
            score.total_score -= 2
            bearish.append(f"Whale sell pressure {100-whale_pressure:.0f}%")
    
    # ══════════════════════════════════════════════
    # ТЕХНИЧЕСКИЕ (+20% вес)
    # ══════════════════════════════════════════════
    
    # RSI
    if rsi is not None:
        if rsi < 35:
            score.technical_score += 2
            score.total_score += 2
            bullish.append(f"RSI {rsi:.0f} < 35: перепроданность")
        elif rsi < 45:
            score.technical_score += 1
            score.total_score += 1
            bullish.append(f"RSI {rsi:.0f} < 45: потенциал роста")
        elif rsi > 75:
            score.technical_score -= 2
            score.total_score -= 2
            bearish.append(f"RSI {rsi:.0f} > 75: перекупленность")
        elif rsi > 70:
            score.technical_score -= 1
            score.total_score -= 1
            bearish.append(f"RSI {rsi:.0f} > 70: перекуплен")
    
    # Price vs MA
    if price_vs_ma50 == "above":
        score.technical_score += 1
        score.total_score += 1
        bullish.append("Цена > MA50: бычий тренд")
    elif price_vs_ma50 == "below":
        score.technical_score -= 1
        score.total_score -= 1
        bearish.append("Цена < MA50: медвежий тренд")
    
    # Trend
    if trend == "UPTREND":
        score.technical_score += 1
        score.total_score += 1
        bullish.append("Тренд: UPTREND")
    elif trend == "DOWNTREND":
        score.technical_score -= 1
        score.total_score -= 1
        bearish.append("Тренд: DOWNTREND")
    
    # ══════════════════════════════════════════════
    # СЕНТИМЕНТ (+20% вес)
    # ══════════════════════════════════════════════
    
    # Fear & Greed (противоположный индикатор)
    if fear_greed is not None:
        if fear_greed < 25:
            score.sentiment_score += 2
            score.total_score += 2
            bullish.append(f"F&G {fear_greed:.0f} < 25: ЭКСТРЕМАЛЬНЫЙ СТРАХ (противоположный)")
        elif fear_greed < 35:
            score.sentiment_score += 1
            score.total_score += 1
            bullish.append(f"F&G {fear_greed:.0f} < 35: СТРАХ")
        elif fear_greed > 70:
            score.sentiment_score -= 2
            score.total_score -= 2
            bearish.append(f"F&G {fear_greed:.0f} > 70: ЭЙФОРИЯ (пузырь)")
        elif fear_greed > 60:
            score.sentiment_score -= 1
            score.total_score -= 1
            bearish.append(f"F&G {fear_greed:.0f} > 60: ЖАДНОСТЬ")
    
    # FinBERT Sentiment
    if sentiment == "BULLISH":
        score.sentiment_score += 1
        score.total_score += 1
        bullish.append("FinBERT: BULLISH")
    elif sentiment == "BEARISH":
        score.sentiment_score -= 1
        score.total_score -= 1
        bearish.append("FinBERT: BEARISH")
    
    # ══════════════════════════════════════════════
    # ПРЕДВАРИТЕЛЬНЫЙ ВЕРДИКТ
    # ══════════════════════════════════════════════
    
    if score.total_score > 3:
        score.preliminary_verdict = "BULLISH"
    elif score.total_score < -3:
        score.preliminary_verdict = "BEARISH"
    else:
        score.preliminary_verdict = "NEUTRAL"
    
    return score


def format_scored_context_for_agents(score: MarketScore) -> str:
    """Форматирует scored данные для AI агентов"""
    
    lines = ["=== 🎯 СИСТЕМА БАЛЛОВ ==="]
    
    # Общий скор
    lines.append(f"Общий балл: **{score.total_score:+d}**")
    
    # По категориям
    lines.append("")
    lines.append("📊 По категориям:")
    lines.append(f"• Макро: {score.macro_score:+d}")
    lines.append(f"• Ончейн: {score.onchain_score:+d}")
    lines.append(f"• Техника: {score.technical_score:+d}")
    lines.append(f"• Сентимент: {score.sentiment_score:+d}")
    
    # Бычьи сигналы
    if score.bullish_signals:
        lines.append("")
        lines.append("🟢 Бычьи сигналы:")
        for sig in score.bullish_signals[:5]:  # Ограничиваем
            lines.append(f"  • {sig}")
    
    # Медвежьи сигналы
    if score.bearish_signals:
        lines.append("")
        lines.append("🔴 Медвежьи сигналы:")
        for sig in score.bearish_signals[:5]:
            lines.append(f"  • {sig}")
    
    # Стоп-факторы
    if score.has_critical_bearish:
        lines.append("")
        lines.append("🚨 КРИТИЧЕСКИЙ СТОП-ФАКТОР: МЕДВЕЖИЙ")
    
    if score.has_critical_bullish:
        lines.append("")
        lines.append("🔵 КРИТИЧЕСКИЙ СТОП-ФАКТОР: БЫЧИЙ")
    
    # Предварительный вердикт
    lines.append("")
    emoji = "🟢" if score.preliminary_verdict == "BULLISH" else "🔴" if score.preliminary_verdict == "BEARISH" else "⚪"
    lines.append(f"{emoji} Предварительный вердикт: **{score.preliminary_verdict}**")
    
    return "\n".join(lines)


def format_signal_block_for_debates(
    score: MarketScore,
    onchain: OnChainMetrics | None = None,
    macro: MacroExtended | None = None,
) -> str:
    """
    Формирует структурированный СИГНАЛ БЛОК для Bull/Bear агентов.
    Агенты должны читать этот блок и использовать в аргументах.
    
    Формат:
    === 🎯 СИГНАЛЫ ДЛЯ ДЕБАТОВ ===
    🟢 БЫЧЬИ:
      • описание + почему бычий
    🔴 МЕДВЕЖИЙ:
      • описание + почему медвежий
    ⚠️ ВНИМАНИЕ:
      • описание
    """
    lines = ["=== 🎯 СИГНАЛЫ ДЛЯ ДЕБАТОВ ==="]
    
    # Определяем бычьи и медвежьи из скора
    bull_list = []
    bear_list = []
    neutral_list = []
    
    if score.bullish_signals:
        for sig in score.bullish_signals[:4]:
            bull_list.append(sig)
    
    if score.bearish_signals:
        for sig in score.bearish_signals[:4]:
            bear_list.append(sig)
    
    # Добавляем критические стоп-факторы
    if score.has_critical_bullish:
        bull_list.append("🔵 ИСТОРИЧЕСКОЕ ДНО / ЭКСТРЕМАЛЬНЫЙ СТРАХ — критический бычий")
    if score.has_critical_bearish:
        bear_list.append("🚨 MVRV>3.5 / VIX>40 / ПУЗЫРЬ — критический медвежий")
    
    # Ончейн данные
    if onchain:
        if onchain.mvrv > 3.5:
            bear_list.append(f"MVRV {onchain.mvrv:.1f} — ПЕРЕОЦЕНЁН (риск коррекции)")
        elif onchain.mvrv < 1.0:
            bull_list.append(f"MVRV {onchain.mvrv:.1f} — ИСТОРИЧЕСКОЕ ДНО (opportunity)")
        elif onchain.mvrv > 3.0:
            neutral_list.append(f"MVRV {onchain.mvrv:.1f} — Высокий (внимание)")
        else:
            bull_list.append(f"MVRV {onchain.mvrv:.1f} — Норма/Справедливо (бычий)")
        
        if onchain.sopr > 1.05:
            bear_list.append(f"SOPR {onchain.sopr:.3f} — Фиксация прибыли (осторожно)")
        elif onchain.sopr < 0.95:
            bull_list.append(f"SOPR {onchain.sopr:.3f} — Капитуляция (дно?)")
        
        if "HODL" in (onchain.exchange_reserves_signal or ""):
            bull_list.append("Exchange Reserves падают — HODLing фаза (🟢 бычий)")
        elif "продажа" in (onchain.exchange_reserves_signal or "").lower():
            bear_list.append("Exchange Reserves растут — продажа (🔴 медвежий)")
        
        if "+" in (onchain.active_addresses_signal or "") or "растёт" in (onchain.active_addresses_signal or "").lower():
            bull_list.append("Active Addresses растут — здоровый рост (🟢 бычий)")
    
    # Макро данные
    if macro:
        if macro.qe_qt_mode == "QE":
            bull_list.append("Fed QE — ликвидность растёт (🟢 бычий)")
        elif macro.qe_qt_mode == "QT":
            bear_list.append("Fed QT — ликвидность падает (🔴 медвежий)")
        
        if macro.yield_spread < -0.5:
            bear_list.append(f"Yield Curve ИНВЕРТИРОВАНА ({macro.yield_spread:.2f}%) — рецессия риск")
        elif macro.yield_spread < 0:
            neutral_list.append(f"Yield Curve частично инвертирована ({macro.yield_spread:.2f}%) — внимание")
        
        if macro.hy_spread > 5.0:
            bear_list.append(f"Credit Spread {macro.hy_spread:.1f}% — СТРЕСС на рынке")
        elif macro.hy_spread > 3.5:
            neutral_list.append(f"Credit Spread повышен ({macro.hy_spread:.1f}%)")
        
        if macro.fed_balance_change_1w > 10:
            bull_list.append(f"Fed Balance +${macro.fed_balance_change_1w:.0f}B/wk — QE режим")
        elif macro.fed_balance_change_1w < -10:
            bear_list.append(f"Fed Balance -${abs(macro.fed_balance_change_1w):.0f}B/wk — QT режим")
    
    # Итоговый вердикт из скора
    verdict_note = ""
    if score.final_verdict == "BULLISH":
        verdict_note = " → СИСТЕМА БАЛЛОВ РЕКОМЕНДУЕТ: БЫЧИЙ"
    elif score.final_verdict == "BEARISH":
        verdict_note = " → СИСТЕМА БАЛЛОВ РЕКОМЕНДУЕТ: МЕДВЕЖИЙ"
    
    # Собираем блок
    if bull_list:
        lines.append("")
        lines.append("🟢 БЫЧЬИ:")
        for sig in bull_list[:5]:
            lines.append(f"  • {sig}")
    
    if bear_list:
        lines.append("")
        lines.append("🔴 МЕДВЕЖИЙ:")
        for sig in bear_list[:5]:
            lines.append(f"  • {sig}")
    
    if neutral_list:
        lines.append("")
        lines.append("⚠️ ВНИМАНИЕ:")
        for sig in neutral_list[:3]:
            lines.append(f"  • {sig}")
    
    if verdict_note:
        lines.append("")
        lines.append(f"📊 {verdict_note}")
    
    return "\n".join(lines)


# ─── Test ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Тест с различными сценариями
    
    print("=== ТЕСТ СКОРИНГА ===\n")
    
    # Сценарий 1: Бычий
    print("--- Сценарий 1: Бычий ---")
    score = calculate_market_score(
        vix=17.5,
        yield_curve_spread=0.3,
        qe_qt_mode="QE",
        mvrv=1.8,
        rsi=55,
        fear_greed=35,
        sentiment="NEUTRAL",
    )
    print(f"Total: {score.total_score:+d}")
    print(f"Verdict: {score.preliminary_verdict}")
    print(f"Details: {score.score_explanation}")
    print()
    
    # Сценарий 2: Медвежий
    print("--- Сценарий 2: Медвежий ---")
    score = calculate_market_score(
        vix=28,
        yield_curve_spread=-0.4,
        qe_qt_mode="QT",
        mvrv=3.8,
        rsi=72,
        fear_greed=60,
    )
    print(f"Total: {score.total_score:+d}")
    print(f"Verdict: {score.preliminary_verdict}")
    print(f"Details: {score.score_explanation}")
    print()
    
    # Сценарий 3: Нейтральный
    print("--- Сценарий 3: Нейтральный ---")
    score = calculate_market_score(
        vix=20,
        mvrv=2.5,
        rsi=55,
        fear_greed=50,
    )
    print(f"Total: {score.total_score:+d}")
    print(f"Verdict: {score.preliminary_verdict}")
    print()
    
    # Тест критических стоп-факторов
    print("--- Критические стоп-факторы ---")
    bear, bull = get_critical_signals(mvrv=0.8)
    print(f"MVRV 0.8: critical_bearish={bear}, critical_bullish={bull}")
    
    bear, bull = get_critical_signals(mvrv=4.0)
    print(f"MVRV 4.0: critical_bearish={bear}, critical_bullish={bull}")
    
    bear, bull = get_critical_signals(vix=45)
    print(f"VIX 45: critical_bearish={bear}, critical_bullish={bull}")
    
    bear, bull = get_critical_signals(fear_greed=20)
    print(f"F&G 20: critical_bearish={bear}, critical_bullish={bull}")
