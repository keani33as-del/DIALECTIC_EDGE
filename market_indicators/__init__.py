"""
data_sources/
Модуль расширенных данных для AI-анализа.

Структура:
├── __init__.py          # Точка входа, экспорт основных функций
├── onchain.py           # On-chain метрики (MVRV, SOPR, Reserves, etc.)
├── macro_extended.py     # Расширенные макро данные (Yields, Balance Sheet)
├── scorer.py             # Система баллов для вердикта
└── aggregator.py         # Сборщик всех данных
"""

from .onchain import (
    fetch_onchain_metrics,
    format_onchain_for_agents,
    get_mvrv_signal,
    get_sopr_signal,
    get_exchange_reserves_signal,
)
from .macro_extended import (
    fetch_extended_macro,
    format_macro_extended_for_agents,
    get_yield_curve_signal,
    get_fed_balance_signal,
)
from .scorer import (
    calculate_market_score,
    get_critical_signals,
    format_scored_context_for_agents,
)
from .aggregator import (
    build_enriched_context,
    enrich_prices_with_scores,
)

__all__ = [
    # On-chain
    "fetch_onchain_metrics",
    "format_onchain_for_agents",
    "get_mvrv_signal",
    "get_sopr_signal",
    "get_exchange_reserves_signal",
    # Macro extended
    "fetch_extended_macro",
    "format_macro_extended_for_agents",
    "get_yield_curve_signal",
    "get_fed_balance_signal",
    # Scoring
    "calculate_market_score",
    "get_critical_signals",
    "format_scored_context_for_agents",
    # Aggregator
    "build_enriched_context",
    "enrich_prices_with_scores",
]
