"""
test_market_indicators.py — Тесты БЕЗ реальных API вызовов
Проверяет логику, форматирование, скоринг
"""

import asyncio
import sys


def test_scorer_logic():
    """Тест системы баллов"""
    from market_indicators.scorer import (
        calculate_market_score,
        get_critical_signals,
        format_scored_context_for_agents,
        MarketScore
    )
    
    print("=" * 50)
    print("TEST 1: Скоринг — Бычий сценарий")
    print("=" * 50)
    
    score = calculate_market_score(
        vix=17.5,
        fed_rate_change="down",
        yield_curve_spread=0.3,
        qe_qt_mode="QE",
        mvrv=1.8,
        sopr=1.02,
        exchange_reserves_trend="down",
        rsi=55,
        price_vs_ma50="above",
        fear_greed=35,
        sentiment="NEUTRAL",
    )
    
    print(f"Total Score: {score.total_score:+d}")
    print(f"Macro Score: {score.macro_score:+d}")
    print(f"OnChain Score: {score.onchain_score:+d}")
    print(f"Tech Score: {score.technical_score:+d}")
    print(f"Sentiment Score: {score.sentiment_score:+d}")
    print(f"Verdict: {score.preliminary_verdict}")
    print(f"Positive Signals: {score.bullish_signals}")
    
    assert score.total_score > 0, "Should be positive"
    assert score.preliminary_verdict == "BULLISH", "Should be bullish"
    print("[OK] PASSED\n")
    
    print("=" * 50)
    print("TEST 2: Скоринг — Медвежий сценарий")
    print("=" * 50)
    
    score = calculate_market_score(
        vix=28,
        fed_rate_change="up",
        yield_curve_spread=-0.4,
        qe_qt_mode="QT",
        mvrv=3.8,
        rsi=72,
        fear_greed=65,
        sentiment="NEUTRAL",
    )
    
    print(f"Total Score: {score.total_score:+d}")
    print(f"Verdict: {score.preliminary_verdict}")
    print(f"Negative Signals: {score.bearish_signals}")
    
    assert score.total_score < 0, "Should be negative"
    assert score.preliminary_verdict == "BEARISH", "Should be bearish"
    print("[OK] PASSED\n")
    
    print("=" * 50)
    print("TEST 3: Скоринг — Нейтральный сценарий")
    print("=" * 50)
    
    score = calculate_market_score(
        vix=20,
        mvrv=2.5,
        rsi=55,
        fear_greed=50,
    )
    
    print(f"Total Score: {score.total_score:+d}")
    print(f"Verdict: {score.preliminary_verdict}")
    
    assert abs(score.total_score) <= 3, "Should be neutral"
    assert score.preliminary_verdict == "NEUTRAL", "Should be neutral"
    print("[OK] PASSED\n")
    
    print("=" * 50)
    print("TEST 4: Критические стоп-факторы")
    print("=" * 50)
    
    # MVRV < 1.0 = автоматически бычий
    bear, bull = get_critical_signals(mvrv=0.8)
    print(f"MVRV 0.8: bearish={bear}, bullish={bull}")
    assert bull == True, "Should trigger bullish stop"
    
    # MVRV > 3.5 = автоматически медвежий
    bear, bull = get_critical_signals(mvrv=4.0)
    print(f"MVRV 4.0: bearish={bear}, bullish={bull}")
    assert bear == True, "Should trigger bearish stop"
    
    # VIX > 40 = кризис
    bear, bull = get_critical_signals(vix=45)
    print(f"VIX 45: bearish={bear}, bullish={bull}")
    assert bear == True, "Should trigger bearish stop"
    
    # Fear & Greed < 25 = экстремальный страх
    bear, bull = get_critical_signals(fear_greed=20)
    print(f"F&G 20: bearish={bear}, bullish={bull}")
    assert bull == True, "Should trigger bullish stop"
    
    print("[OK] PASSED\n")


def test_formatters():
    """Тест форматирования"""
    from market_indicators.scorer import MarketScore, format_scored_context_for_agents
    from market_indicators.onchain import OnChainMetrics, format_onchain_for_agents
    from market_indicators.macro_extended import MacroExtended, format_macro_extended_for_agents
    
    print("=" * 50)
    print("TEST 5: Форматирование скора")
    print("=" * 50)
    
    score = MarketScore(
        total_score=5,
        macro_score=2,
        onchain_score=1,
        technical_score=1,
        sentiment_score=1,
        bullish_signals=["VIX < 20", "Цена > MA50"],
        bearish_signals=["RSI > 70"],
    )
    
    formatted = format_scored_context_for_agents(score)
    # Skip print (has emoji)
    assert "Общий балл: **+5**" in formatted
    assert "VIX < 20" in formatted
    assert "Цена > MA50" in formatted
    print("[OK] PASSED\n")
    
    print("=" * 50)
    print("TEST 6: Format on-chain")
    print("=" * 50)
    
    metrics = OnChainMetrics(
        mvrv=2.5,
        mvrv_signal="Normal (2.0-3.0)",
        sopr=1.03,
        sopr_signal="Normal (SOPR=1.030)",
        reserves_signal="HODLing phase",
        tx_volume_24h=28.5,
    )
    
    formatted = format_onchain_for_agents(metrics)
    # Skip print (has emoji)
    assert "MVRV" in formatted
    assert "SOPR" in formatted
    assert "24h Volume" in formatted
    print("[OK] PASSED\n")
    
    print("=" * 50)
    print("TEST 7: Format macro")
    print("=" * 50)
    
    macro = MacroExtended(
        fed_balance_billions=7380,
        fed_balance_signal="NEUTRAL (balance stable $7380B)",
        qe_qt_mode="NEUTRAL",
        yield_10y=4.52,
        yield_2y=4.98,
        yield_spread=-0.46,
        yield_curve_signal="PARTIALLY INVERTED (-0.46%)",
        hy_spread=3.2,
        credit_signal="NORMAL (3.2%)",
    )
    
    formatted = format_macro_extended_for_agents(macro)
    # Skip print (has emoji)
    assert "Fed Balance" in formatted
    assert "10Y Yield" in formatted
    assert "Yield Curve" in formatted
    assert "Credit Spread" in formatted
    print("[OK] PASSED\n")


def test_scoring_edge_cases():
    """Тест edge cases"""
    from market_indicators.scorer import calculate_market_score
    
    print("=" * 50)
    print("TEST 8: Edge cases — пустые данные")
    print("=" * 50)
    
    score = calculate_market_score()
    print(f"Total Score (no data): {score.total_score:+d}")
    print(f"Verdict: {score.preliminary_verdict}")
    
    assert score.total_score == 0, "Should be zero"
    assert score.preliminary_verdict == "NEUTRAL", "Should be neutral"
    print("[OK] PASSED\n")
    
    print("=" * 50)
    print("TEST 9: Edge cases — конфликтующие сигналы")
    print("=" * 50)
    
    # Бычий on-chain, медвежий макро
    score = calculate_market_score(
        vix=30,  # медвежий
        qe_qt_mode="QT",  # медвежий
        mvrv=0.9,  # бычий
        fear_greed=20,  # бычий
        rsi=35,  # бычий
    )
    
    print(f"Total Score: {score.total_score:+d}")
    print(f"Macro: {score.macro_score:+d}, OnChain: {score.onchain_score:+d}, Tech: {score.technical_score:+d}, Sent: {score.sentiment_score:+d}")
    print(f"Verdict: {score.preliminary_verdict}")
    print(f"Bullish: {score.bullish_signals}")
    print(f"Bearish: {score.bearish_signals}")
    
    # Макро должен перевесить
    assert score.macro_score < 0, "Macro should be bearish"
    print("[OK] PASSED\n")


def test_enriched_data_structure():
    """Тест структуры данных"""
    from market_indicators.aggregator import EnrichedData
    from market_indicators.scorer import MarketScore
    from market_indicators.onchain import OnChainMetrics
    from market_indicators.macro_extended import MacroExtended
    
    print("=" * 50)
    print("TEST 10: Структура EnrichedData")
    print("=" * 50)
    
    enriched = EnrichedData()
    
    assert enriched.onchain is not None
    assert enriched.macro is not None
    assert enriched.score is not None
    
    # Заполняем данными
    enriched.onchain.mvrv = 2.5
    enriched.macro.yield_spread = -0.3
    enriched.score.total_score = 3
    enriched.score.preliminary_verdict = "BULLISH"
    
    assert enriched.onchain.mvrv == 2.5
    assert enriched.macro.yield_spread == -0.3
    assert enriched.score.total_score == 3
    
    print(f"MVRV: {enriched.onchain.mvrv}")
    print(f"Yield Spread: {enriched.macro.yield_spread}")
    print(f"Final Verdict: {enriched.score.final_verdict}")
    print("[OK] PASSED\n")


def test_enriched_data_structure():
    """Тест структуры EnrichedData"""
    from market_indicators.aggregator import EnrichedData
    from market_indicators.scorer import MarketScore
    
    print("=" * 50)
    print("TEST: EnrichedData структура")
    print("=" * 50)
    
    enriched = EnrichedData()
    assert enriched.onchain is not None
    assert enriched.macro is not None
    assert enriched.score is not None
    print(f"MVRV: {enriched.onchain.mvrv}")
    print(f"Yield Spread: {enriched.macro.yield_spread}")
    print(f"Final Verdict: {enriched.score.final_verdict}")
    print("[OK] PASSED\n")


def test_hallucination_tracking():
    """Тест трекинга галлюцинаций — без API вызовов"""
    import re as _re
    
    print("=" * 50)
    print("TEST: Hallucination Tracking")
    print("=" * 50)
    
    from ai_provider import _hallucination_stats, track_hallucinations, get_hallucination_report
    
    _hallucination_stats["bull"]["total"] = 0
    _hallucination_stats["bull"]["hall"] = 0
    _hallucination_stats["bull"]["by_model"] = {}
    _hallucination_stats["bear"]["total"] = 0
    _hallucination_stats["bear"]["hall"] = 0
    
    bull_content = ("• BTC: signal\n• ETH: bychiy\n• SOL: neytral\n• F&G 35: strakh")
    bear_content = ("• MVRV 3.2: vysokiy\n• QT: likvidnost\n• SPX: volatilnost")
    
    bull_args = max(1, len([p for p in bull_content.split("\n") if p.strip().startswith("•")]))
    bear_args = max(1, len([p for p in bear_content.split("\n") if p.strip().startswith("•")]))
    
    track_hallucinations("bull", bull_args, 2, "test-bull-model")
    track_hallucinations("bear", bear_args, 1, "test-bear-model")
    
    report = get_hallucination_report()
    
    print(f"Bull: {report['bull']['hallucinations']}/{report['bull']['total_args']} ({report['bull']['rate_pct']:.1f}%)")
    print(f"Bear: {report['bear']['hallucinations']}/{report['bear']['total_args']} ({report['bear']['rate_pct']:.1f}%)")
    
    assert report["bull"]["total_args"] == 4
    assert report["bull"]["hallucinations"] == 2
    assert report["bull"]["rate_pct"] == 50.0
    assert report["bear"]["total_args"] == 3
    assert report["bear"]["hallucinations"] == 1
    assert abs(report["bear"]["rate_pct"] - 33.3) < 1
    print("[OK] PASSED\n")
    
    print("=" * 50)
    print("TEST 2: Per-model tracking")
    print("=" * 50)
    
    _hallucination_stats["bull"]["total"] = 0
    _hallucination_stats["bull"]["hall"] = 0
    _hallucination_stats["bull"]["by_model"] = {}
    
    track_hallucinations("bull", 10, 1, "good-model")
    track_hallucinations("bull", 10, 3, "bad-model")
    
    report2 = get_hallucination_report()
    print(f"Overall: {report2['bull']['hallucinations']}/{report2['bull']['total_args']} ({report2['bull']['rate_pct']:.1f}%)")
    for m, d in report2["bull"]["by_model"].items():
        m_rate = d.get("rate", d.get("rate_pct", 0))
        print(f"  -> {m}: {d['hallucinations']}/{d['total_args']} ({m_rate:.1f}%)")
    
    assert report2["bull"]["total_args"] == 20
    assert report2["bull"]["hallucinations"] == 4
    assert report2["bull"]["rate_pct"] == 20.0
    good = report2["bull"]["by_model"].get("good-model", {})
    bad = report2["bull"]["by_model"].get("bad-model", {})
    assert good.get("rate", good.get("rate_pct", 0)) == 10.0
    assert bad.get("rate", bad.get("rate_pct", 0)) == 30.0
    print("[OK] PASSED\n")
    
    print("=" * 50)
    print("TEST 2: Модель с низким hallucination rate")
    print("=" * 50)
    
    # Сбрасываем
    _hallucination_stats["bull"]["total"] = 0
    _hallucination_stats["bull"]["hall"] = 0
    _hallucination_stats["bull"]["by_model"] = {}
    
    # Хорошая модель: 1 ошибка из 10
    track_hallucinations("bull", 10, 1, "good-model")
    track_hallucinations("bull", 10, 3, "bad-model")
    
    report2 = get_hallucination_report()
    print(f"Overall: {report2['bull']['hallucinations']}/{report2['bull']['total_args']} ({report2['bull']['rate_pct']:.1f}%)")
    for m, d in report2["bull"]["by_model"].items():
        print(f"  -> {m}: {d['hallucinations']}/{d['total_args']} ({d.get('rate', d.get('rate_pct', 0)):.1f}%)")
    
    assert report2["bull"]["total_args"] == 20
    assert report2["bull"]["hallucinations"] == 4
    assert report2["bull"]["rate_pct"] == 20.0
    
    good = report2["bull"]["by_model"].get("good-model", {})
    bad = report2["bull"]["by_model"].get("bad-model", {})
    assert good.get("rate", good.get("rate_pct", 0)) == 10.0, f"Good model should be 10%, got {good.get('rate')}"
    assert bad.get("rate", bad.get("rate_pct", 0)) == 30.0, f"Bad model should be 30%, got {bad.get('rate')}"
    
    print("[OK] PASSED\n")


def main():
    print("\n" + "=" * 60)
    print("RUNNING TESTS (NO API CALLS)")
    print("=" * 60 + "\n")
    
    try:
        # Тесты скоринга
        test_scorer_logic()
        
        # Тесты форматирования
        test_formatters()
        
        # Edge cases
        test_scoring_edge_cases()
        
        # Структуры данных
        test_enriched_data_structure()
        
        # Hallucination tracking (БЕЗ API)
        test_hallucination_tracking()
        
        print("\n" + "=" * 60)
        print("ALL TESTS PASSED!")
        print("=" * 60)
        print("\nLogic works. Ready to deploy!")
        return 0
        
    except AssertionError as e:
        print(f"\n[FAIL] ASSERTION FAILED: {e}")
        return 1
    except Exception as e:
        print(f"\n[FAIL] ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
