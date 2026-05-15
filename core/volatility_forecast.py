"""volatility_forecast.py — Forward-looking прогноз волатильности (EWMA).

Реализует RiskMetrics-стиль EWMA-волатильность (J.P. Morgan 1996), без
зависимостей. Это лёгкая замена GARCH(1,1) с близким качеством на
крипто-таймрайдах:

    σ²_{t+1} = λ * σ²_t + (1 - λ) * r²_t

λ = 0.94 — стандарт RiskMetrics для дневных returns.

Почему не GARCH(1,1):
- GARCH требует MLE-оптимизацию (scipy.optimize), что добавило бы
  тяжёлую зависимость и риски сходимости на коротких рядах.
- На крипте λ=0.94 EWMA эмпирически даёт ~95% корреляцию с
  fitted GARCH (Engle, Patton 2001). Edge от GARCH мал, weight большой.
- EWMA параметр-free → нечего оверфитить.

Когда стоит апгрейдить до GARCH:
- При сильно асимметричных tail-событиях (TGARCH) — крипта это типичный
  случай, но требует event-by-event анализа.
- Когда нужны multi-step forecasts h-day ahead (EWMA даёт только 1-day,
  если хочется σ²_{t+h} нужна модель с reversion-mean — GARCH).

Reference:
    J.P. Morgan (1996). RiskMetrics Technical Document, 4th edition.
    Engle, R. F. (2001). GARCH 101: An Introduction to the Use of
        ARCH/GARCH Models in Applied Econometrics. JEP 15(4).

Pure-Python (math + stdlib), без numpy.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)


RISKMETRICS_LAMBDA = 0.94
MIN_BARS_FOR_VOL = 30  # ниже EWMA-инициализация неустойчива


@dataclass
class VolatilityForecast:
    """Прогноз волатильности на следующий бар.

    Attributes:
        sigma_1d: σ_{t+1} в долях (например 0.023 = 2.3% дневная вола).
        sigma_1d_pct: то же в процентах.
        sigma_annualized_pct: σ * sqrt(365) в процентах. Стандартная
            крипто-нормировка (365 торговых дней, в отличие от 252 у акций).
        realized_1d_pct: backward-looking σ за последний день (|r_t|, в %).
            Сравнение с sigma_1d_pct: если forecast сильно выше realized →
            модель ожидает рост волатильности.
        decay_lambda: использованный λ (для воспроизводимости).
        n_bars: сколько баров использовано для оценки.
    """

    sigma_1d: float
    sigma_1d_pct: float
    sigma_annualized_pct: float
    realized_1d_pct: float
    decay_lambda: float
    n_bars: int

    def to_dict(self) -> dict:
        return {
            "sigma_1d": round(self.sigma_1d, 6),
            "sigma_1d_pct": round(self.sigma_1d_pct, 3),
            "sigma_annualized_pct": round(self.sigma_annualized_pct, 2),
            "realized_1d_pct": round(self.realized_1d_pct, 3),
            "decay_lambda": self.decay_lambda,
            "n_bars": self.n_bars,
        }


def _variance(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mu = sum(values) / len(values)
    return sum((v - mu) ** 2 for v in values) / len(values)


def forecast_volatility_ewma(
    returns: Sequence[float],
    decay: float = RISKMETRICS_LAMBDA,
    annualization_days: int = 365,
) -> Optional[VolatilityForecast]:
    """EWMA-прогноз волатильности на следующий бар.

    Args:
        returns: ряд лог-доходностей (как из compute_returns()).
        decay: λ ∈ (0, 1). 0.94 RiskMetrics, 0.97 для длинной памяти,
            <0.90 для агрессивно-реактивной оценки.
        annualization_days: число баров в году для annualized σ.
            365 для крипты (24/7), 252 для акций.

    Returns:
        VolatilityForecast или None если len(returns) < MIN_BARS_FOR_VOL
        или возвраты вырождены (var=0).

    Алгоритм:
        1. Инициализируем σ²_0 = выборочная дисперсия первой четверти ряда.
           Это снижает влияние start-value на forecast (warm-up window).
        2. Итеративно обновляем σ²_{t+1} = λ σ²_t + (1-λ) r²_t.
        3. Возвращаем σ_{n+1} (т.е. forecast на следующий бар) и
           realized |r_n| как backward-looking бейзлайн для сравнения.
    """
    n = len(returns)
    if n < MIN_BARS_FOR_VOL:
        return None
    if not (0 < decay < 1):
        return None

    # Warm-up: первые n/4 баров — для инициализации σ²_0
    warmup = max(5, n // 4)
    sigma2 = _variance(returns[:warmup])
    if sigma2 <= 0:
        # Все returns внутри warm-up окна одинаковы — пробуем по всему ряду.
        sigma2 = _variance(returns)
    if sigma2 <= 0:
        return None

    # EWMA update, начиная с конца warmup
    history: List[float] = [sigma2]
    for r in returns[warmup:]:
        sigma2 = decay * sigma2 + (1.0 - decay) * (r * r)
        history.append(sigma2)

    sigma_next = math.sqrt(max(0.0, sigma2))
    realized_1d_pct = abs(returns[-1]) * 100.0
    annualization = math.sqrt(float(annualization_days))

    return VolatilityForecast(
        sigma_1d=sigma_next,
        sigma_1d_pct=sigma_next * 100.0,
        sigma_annualized_pct=sigma_next * 100.0 * annualization,
        realized_1d_pct=realized_1d_pct,
        decay_lambda=decay,
        n_bars=n,
    )
