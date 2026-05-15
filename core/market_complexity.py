"""
market_complexity.py — Метрики "торгуемости" рынка.

Цель модуля — ответить на вопрос «стоит ли вообще торговать сейчас, или рынок
в режиме случайного блуждания, где любая стратегия = монетка».

Реализует две математически независимые метрики:

1. **Тест Херста (R/S анализ)** — насколько ряд приращений автокоррелирован.
   - H > 0.55 → персистентный (трендовый), MA-стратегии работают
   - H ≈ 0.50 → случайное блуждание, сигналы НЕ работают
   - H < 0.45 → антиперсистентный (mean-reverting), развороты чаще

2. **Энтропия Шеннона** — насколько ряд приращений упорядочен.
   - Низкая (< 0.6 от max) → паттерны работают, рынок предсказуем
   - Высокая (> 0.85 от max) → хаос, не торговать

Оба показателя используются как мягкий фильтр поверх детерминированного
regime_detector.py:
  - Если оба говорят «случайно/хаос» → даже при UPTREND-режиме confidence режется
  - Если оба говорят «упорядочен/трендовый» → confidence можно бустить

ВАЖНО: эти метрики НЕ предсказывают направление. Они отвечают на вопрос
«сейчас в принципе можно торговать по сигналам или это монетка?». Это
дисциплинированный фильтр, не predictive model.

Источники:
  - Mandelbrot & Wallis (1969), R/S analysis
  - Shannon (1948), A Mathematical Theory of Communication
  - Peters (1991), Chaos and Order in the Capital Markets

Без внешних зависимостей (только math) — pure-Python для надёжности.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)


# ── Константы ───────────────────────────────────────────────────────────────

# Минимум баров для надёжного R/S — на меньших Hurst шумный
MIN_BARS_FOR_HURST = 64

# Минимум баров для энтропии — нужно набрать гистограмму
MIN_BARS_FOR_ENTROPY = 32

# Пороги интерпретации Hurst
HURST_TRENDING_THRESHOLD = 0.55   # выше — трендовый рынок
HURST_RANDOM_LOW = 0.45           # H ∈ [0.45, 0.55] — случайное блуждание
HURST_RANDOM_HIGH = 0.55

# Пороги интерпретации энтропии (нормированной к max=1.0)
# КАЛИБРОВКА: на лог-returns финансовых рядов энтропия по 10 бинам обычно
# 0.85–0.92 даже в чистом тренде (приращения ~нормальные → распределение
# по бинам относительно равномерное). Поэтому пороги сдвинуты «вверх»
# относительно теоретических — они эмпирические для крипты/equity.
ENTROPY_ORDERED_THRESHOLD = 0.82  # ниже — упорядочен (редко на крипте)
ENTROPY_CHAOS_THRESHOLD = 0.94    # выше — реальный хаос (event days)


# ── Dataclass ───────────────────────────────────────────────────────────────


@dataclass
class MarketComplexity:
    """Метрики торгуемости рынка.

    Attributes:
        hurst: показатель Херста, 0..1 (≈0.5 = случайное блуждание).
        entropy_normalized: нормированная энтропия Шеннона, 0..1.
            0 = полная предсказуемость, 1 = равновероятный шум.
        tradeable_score: интегральная оценка торгуемости, 0..1.
            >0.7 = сигналы должны работать, торгуй; <0.3 = хаос, не торгуй.
        regime_hint: текстовая подсказка по режиму (TRENDING/RANDOM_WALK/
            MEAN_REVERTING/CHAOTIC/ORDERED).
        recommendation: actionable рекомендация на русском.
    """

    hurst: float
    entropy_normalized: float
    tradeable_score: float
    regime_hint: str
    recommendation: str

    def to_dict(self) -> dict:
        return {
            "hurst": round(self.hurst, 3),
            "entropy_normalized": round(self.entropy_normalized, 3),
            "tradeable_score": round(self.tradeable_score, 3),
            "regime_hint": self.regime_hint,
            "recommendation": self.recommendation,
        }


# ── Математика: Hurst (R/S метод) ───────────────────────────────────────────


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    var = sum((v - m) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(var)


def _rescaled_range(series: Sequence[float]) -> float:
    """R/S — rescaled range для одного окна.

    R = max(cumsum) - min(cumsum), где cumsum = накопленная сумма (x_i - mean).
    S = std(series). R/S = R / S.
    """
    n = len(series)
    if n < 2:
        return 0.0

    mean_val = _mean(series)
    deviations = [v - mean_val for v in series]

    # Накопленная сумма отклонений
    cum = 0.0
    cum_min = 0.0
    cum_max = 0.0
    for d in deviations:
        cum += d
        if cum < cum_min:
            cum_min = cum
        if cum > cum_max:
            cum_max = cum

    R = cum_max - cum_min
    S = _stdev(series)
    if S < 1e-12:
        return 0.0
    return R / S


def hurst_exponent(returns: Sequence[float], min_chunk: int = 8) -> Optional[float]:
    """Hurst exponent через R/S analysis.

    Args:
        returns: ряд лог-приращений (или просто returns — главное, чтобы был
            stationary, не цены напрямую). Лучше всего — log_returns.
        min_chunk: минимальный размер чанка для R/S (8 — стандарт у Peters).

    Returns:
        H ∈ (0, 1) или None если данных недостаточно.

    Метод:
        1. Делим ряд на чанки разных размеров (min_chunk, 2*min_chunk, 4*...).
        2. Для каждого размера считаем средний R/S по всем чанкам.
        3. Линейная регрессия log(R/S) vs log(n) — наклон = Hurst.

    Замечания:
        Метод чувствителен к коротким рядам — на n<64 даёт шум. Поэтому
        вход возвращаем None если len(returns) < MIN_BARS_FOR_HURST.
    """
    n = len(returns)
    if n < MIN_BARS_FOR_HURST:
        return None

    # Размеры чанков: min_chunk, 2*min, 4*min, ... до n//2
    chunk_sizes: List[int] = []
    size = min_chunk
    while size <= n // 2:
        chunk_sizes.append(size)
        size *= 2

    if len(chunk_sizes) < 3:
        # Слишком мало точек для регрессии
        return None

    log_n: List[float] = []
    log_rs: List[float] = []

    for chunk_size in chunk_sizes:
        # Делим ряд на непересекающиеся чанки заданного размера
        rs_values: List[float] = []
        for start in range(0, n - chunk_size + 1, chunk_size):
            chunk = returns[start : start + chunk_size]
            rs = _rescaled_range(chunk)
            if rs > 0:
                rs_values.append(rs)

        if not rs_values:
            continue

        avg_rs = _mean(rs_values)
        if avg_rs <= 0:
            continue

        log_n.append(math.log(chunk_size))
        log_rs.append(math.log(avg_rs))

    if len(log_n) < 3:
        return None

    # Линейная регрессия log_rs = H * log_n + c (метод наименьших квадратов)
    n_points = len(log_n)
    sum_x = sum(log_n)
    sum_y = sum(log_rs)
    sum_xy = sum(x * y for x, y in zip(log_n, log_rs))
    sum_xx = sum(x * x for x in log_n)

    denom = n_points * sum_xx - sum_x * sum_x
    if abs(denom) < 1e-12:
        return None

    hurst = (n_points * sum_xy - sum_x * sum_y) / denom

    # Кламп в разумные пределы (теоретически H ∈ [0,1])
    return max(0.0, min(1.0, hurst))


# ── Математика: Shannon entropy ─────────────────────────────────────────────


def shannon_entropy(returns: Sequence[float], bins: int = 10) -> Optional[float]:
    """Энтропия Шеннона ряда returns (нормированная к [0, 1]).

    Args:
        returns: ряд приращений.
        bins: количество корзин для дискретизации (10 — баланс шум/точность).

    Returns:
        Нормированная энтропия 0..1, где:
            0 = все returns в одной корзине (полная предсказуемость)
            1 = равномерное распределение (максимальный хаос)
        None если данных мало или ряд вырожденный.

    Метод:
        1. Дискретизируем returns в `bins` равных корзин (по min/max диапазону).
        2. Считаем p_i = count_i / N для каждой корзины.
        3. H = -Σ p_i * log2(p_i).
        4. Нормируем H / log2(bins) → [0, 1].
    """
    n = len(returns)
    if n < MIN_BARS_FOR_ENTROPY:
        return None

    r_min = min(returns)
    r_max = max(returns)
    if r_max - r_min < 1e-12:
        # Все returns одинаковые — энтропия нулевая
        return 0.0

    bin_width = (r_max - r_min) / bins
    counts = [0] * bins
    for r in returns:
        idx = int((r - r_min) / bin_width)
        if idx >= bins:
            idx = bins - 1
        counts[idx] += 1

    # Энтропия Шеннона
    h = 0.0
    for c in counts:
        if c == 0:
            continue
        p = c / n
        h -= p * math.log2(p)

    # Нормировка к [0, 1] делением на log2(bins) — максимум при равномерном
    h_max = math.log2(bins)
    if h_max < 1e-12:
        return None
    return min(1.0, h / h_max)


# ── Высокоуровневое API ─────────────────────────────────────────────────────


def compute_returns(closes: Sequence[float]) -> List[float]:
    """Лог-приращения из ряда цен закрытия.

    log_return[i] = ln(close[i] / close[i-1])
    Используем именно лог-returns (не простые), потому что они аддитивны
    и лучше моделируются как stationary series.
    """
    if len(closes) < 2:
        return []
    out: List[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        curr = closes[i]
        if prev <= 0 or curr <= 0:
            continue
        out.append(math.log(curr / prev))
    return out


def _interpret(hurst: Optional[float], entropy: Optional[float]) -> tuple[str, str, float]:
    """Возвращает (regime_hint, recommendation, tradeable_score)."""
    # Если ни одна метрика не вычислилась — не вмешиваемся
    if hurst is None and entropy is None:
        return (
            "UNKNOWN",
            "Недостаточно данных для оценки торгуемости (нужно ≥64 бара).",
            0.5,
        )

    # Расшифровка Hurst
    if hurst is None:
        hurst_label = "unknown"
        hurst_score = 0.5
    elif hurst > HURST_TRENDING_THRESHOLD:
        hurst_label = "trending"
        # Чем дальше от 0.5, тем выше score (max 1.0 при Hurst=0.7+)
        hurst_score = min(1.0, 0.5 + (hurst - 0.5) * 2.5)
    elif hurst < HURST_RANDOM_LOW:
        hurst_label = "mean_reverting"
        # Mean-reverting тоже даёт edge для контртрендовых стратегий, но
        # MA-логика бота работает хуже — даём средний score
        hurst_score = 0.55
    else:
        hurst_label = "random_walk"
        # H ≈ 0.5 — самое опасное состояние, монетка
        hurst_score = 0.25

    # Расшифровка энтропии
    if entropy is None:
        entropy_label = "unknown"
        entropy_score = 0.5
    elif entropy < ENTROPY_ORDERED_THRESHOLD:
        entropy_label = "ordered"
        entropy_score = 1.0 - entropy  # чем ниже энтропия, тем выше score
    elif entropy > ENTROPY_CHAOS_THRESHOLD:
        entropy_label = "chaotic"
        entropy_score = 0.20
    else:
        entropy_label = "neutral"
        entropy_score = 0.55

    # Интегральный score: среднее с лёгким даунбиасом если хоть один сигнал плохой
    tradeable_score = (hurst_score + entropy_score) / 2.0

    # Если оба плохие — режем сильнее (мультипликативный штраф)
    if hurst_label == "random_walk" and entropy_label == "chaotic":
        tradeable_score *= 0.5

    # Если оба хорошие — слегка бустим
    if hurst_label == "trending" and entropy_label == "ordered":
        tradeable_score = min(1.0, tradeable_score * 1.15)

    # Сводный лейбл режима
    if hurst_label == "trending" and entropy_label in ("ordered", "neutral"):
        regime_hint = "TRENDING"
        recommendation = (
            f"Hurst={hurst:.2f} (тренд), энтропия={entropy:.2f} ({entropy_label}). "
            "Сигналы по MA должны работать. Можно торговать стандартно."
        )
    elif hurst_label == "mean_reverting" and entropy_label in ("ordered", "neutral"):
        regime_hint = "MEAN_REVERTING"
        recommendation = (
            f"Hurst={hurst:.2f} (mean-reverting). Тренды быстро разворачиваются — "
            "будь осторожнее с пробоями, лучше торговать от границ диапазона."
        )
    elif hurst_label == "random_walk" or entropy_label == "chaotic":
        regime_hint = "RANDOM_WALK" if hurst_label == "random_walk" else "CHAOTIC"
        recommendation = (
            f"Hurst={hurst if hurst is not None else '?':.2f}, "
            f"энтропия={entropy if entropy is not None else '?':.2f}. "
            "Рынок в режиме случайного блуждания / хаоса. "
            "СИГНАЛЫ НЕ РАБОТАЮТ. Лучше отдохнуть."
        )
    else:
        regime_hint = "MIXED"
        recommendation = (
            f"Смешанный режим (Hurst={hurst if hurst is not None else '?':.2f}, "
            f"энтропия={entropy if entropy is not None else '?':.2f}). "
            "Уменьшенные позиции, ждать чёткого подтверждения."
        )

    return regime_hint, recommendation, max(0.0, min(1.0, tradeable_score))


def analyze_complexity(closes: Sequence[float]) -> Optional[MarketComplexity]:
    """Главная точка входа: ряд цен закрытия → MarketComplexity.

    Args:
        closes: ряд цен закрытия (последняя цена — последний элемент).
            Минимум ~65 баров для надёжного Hurst, ~33 для энтропии.

    Returns:
        MarketComplexity с интерпретацией, или None если данных совсем мало
        (< MIN_BARS_FOR_ENTROPY+1).

    Пример использования:
        >>> closes = [c.close for c in candles_1d_last_180]
        >>> complexity = analyze_complexity(closes)
        >>> if complexity and complexity.tradeable_score < 0.3:
        ...     print("не торгуем сегодня")
    """
    if not closes or len(closes) < MIN_BARS_FOR_ENTROPY + 1:
        return None

    returns = compute_returns(closes)
    if len(returns) < MIN_BARS_FOR_ENTROPY:
        return None

    try:
        hurst = hurst_exponent(returns)
    except Exception as e:
        logger.warning("hurst_exponent failed: %s", e)
        hurst = None

    try:
        entropy = shannon_entropy(returns)
    except Exception as e:
        logger.warning("shannon_entropy failed: %s", e)
        entropy = None

    regime_hint, recommendation, tradeable_score = _interpret(hurst, entropy)

    return MarketComplexity(
        hurst=hurst if hurst is not None else 0.5,
        entropy_normalized=entropy if entropy is not None else 0.5,
        tradeable_score=tradeable_score,
        regime_hint=regime_hint,
        recommendation=recommendation,
    )
