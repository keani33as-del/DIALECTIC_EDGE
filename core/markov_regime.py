"""markov_regime.py — Дискретная цепь Маркова на квантизированных доходностях.

Лёгкая альтернатива полноценному HMM, без зависимостей. Идея:

1. Квантизуем дневные лог-доходности в 3 состояния (DOWN/FLAT/UP) по
   квантилям 33%/67%. Это даёт примерно равномерное эмпирическое
   распределение наблюдений, что хорошо для устойчивости перехода.
2. Строим эмпирическую матрицу переходов P[i][j] = P(s_{t+1}=j | s_t=i).
3. Считаем стационарное распределение π как left-eigenvector P для λ=1.
4. Из диагонали матрицы перехода считаем ожидаемое «время жизни» режима
   E[dwell_i] = 1 / (1 - P[i][i]) (геометрическое распределение).

Что эта модель *не* делает:
- Не моделирует Gaussian observations внутри состояния (это уже HMM с
  Baum-Welch). Для свинг-горизонта 3-bucket дискретизации достаточно.
- Не пытается предсказать величину следующего изменения, только знак.
- Не учитывает экзогенные переменные (sentiment, funding) — это next-level.

Полезные применения сигнала:
- next_step_probs дают агентам P(вверх завтра | текущий day был FLAT).
- expected_dwell_bars говорит «сколько ещё типично проживёт текущий
  режим», что useful для horizon-планирования.

Reference:
    Norris, J. R. (1998). Markov Chains. Cambridge University Press.
    Hamilton, J. D. (1989). A New Approach to the Economic Analysis of
        Nonstationary Time Series and the Business Cycle. Econometrica.

Pure-Python, без numpy: модуль market_complexity.py тоже pure-Python,
держим симметрию (легче деплоить на slim Docker).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


STATE_NAMES: Tuple[str, str, str] = ("DOWN", "FLAT", "UP")
MIN_BARS_FOR_MARKOV = 40  # ниже — оценка матрицы переходов шумная


@dataclass
class MarkovRegimeResult:
    """Снимок цепи Маркова на текущем ряду.

    Attributes:
        current_state: текущее состояние (последний бар).
        transition_matrix: 3x3, transition_matrix[i][j] = P(s'=j | s=i).
            Строки нумеруются STATE_NAMES (DOWN/FLAT/UP).
        stationary_distribution: long-run распределение по состояниям.
            sum(π_i) = 1.
        expected_dwell_bars: ожидаемая средняя длина серии в каждом
            состоянии (E[T] = 1/(1-P_ii) под геометрическим предположением).
        next_step_probs: dict состояние→вероятность для НЕПОСРЕДСТВЕННО
            следующего бара (строка матрицы перехода для current_state).
        quantile_thresholds: (q33, q67) — пороги дискретизации returns.
    """

    current_state: str
    transition_matrix: List[List[float]]
    stationary_distribution: Dict[str, float]
    expected_dwell_bars: Dict[str, float]
    next_step_probs: Dict[str, float]
    quantile_thresholds: Tuple[float, float]

    def to_dict(self) -> dict:
        return {
            "current_state": self.current_state,
            "transition_matrix": [
                [round(p, 4) for p in row] for row in self.transition_matrix
            ],
            "stationary_distribution": {
                k: round(v, 4) for k, v in self.stationary_distribution.items()
            },
            "expected_dwell_bars": {
                k: round(v, 2) for k, v in self.expected_dwell_bars.items()
            },
            "next_step_probs": {
                k: round(v, 4) for k, v in self.next_step_probs.items()
            },
            "quantile_thresholds": [
                round(self.quantile_thresholds[0], 6),
                round(self.quantile_thresholds[1], 6),
            ],
        }


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile на отсортированном массиве."""
    if not sorted_values:
        return 0.0
    if q <= 0:
        return sorted_values[0]
    if q >= 1:
        return sorted_values[-1]
    n = len(sorted_values)
    pos = q * (n - 1)
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return sorted_values[low]
    frac = pos - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


def _quantize(returns: Sequence[float], q_low: float, q_high: float) -> List[int]:
    """Квантизация в три состояния: 0=DOWN, 1=FLAT, 2=UP."""
    out: List[int] = []
    for r in returns:
        if r < q_low:
            out.append(0)
        elif r > q_high:
            out.append(2)
        else:
            out.append(1)
    return out


def _stationary_distribution(P: List[List[float]], iters: int = 1000) -> List[float]:
    """π такая, что π·P = π. Считаем power-iteration: π_{n+1} = π_n · P.

    Сходится экспоненциально для эргодических цепей. 1000 итераций > чем
    нужно для 3-state цепи (обычно достаточно 50).
    """
    n = len(P)
    pi = [1.0 / n] * n
    for _ in range(iters):
        new_pi = [0.0] * n
        for j in range(n):
            for i in range(n):
                new_pi[j] += pi[i] * P[i][j]
        s = sum(new_pi)
        if s <= 0:
            break
        new_pi = [x / s for x in new_pi]
        # Эвристика сходимости — на маленьких цепях быстрая.
        if max(abs(new_pi[i] - pi[i]) for i in range(n)) < 1e-10:
            pi = new_pi
            break
        pi = new_pi
    return pi


def analyze_markov_regime(
    returns: Sequence[float],
    quantiles: Tuple[float, float] = (1.0 / 3.0, 2.0 / 3.0),
) -> Optional[MarkovRegimeResult]:
    """Строит цепь Маркова на квантизованных дневных доходностях.

    Args:
        returns: лог-доходности из core.market_complexity.compute_returns().
        quantiles: (q_low, q_high) для дискретизации в DOWN/FLAT/UP.
            По умолчанию терцили — даёт equal-weight состояния.

    Returns:
        MarkovRegimeResult или None если данных < MIN_BARS_FOR_MARKOV.

    Граничные случаи:
        - Если в какой-то строке матрицы переходов 0 наблюдений
          (например DOWN никогда не встречался), эта строка равномерно
          раскладывается (1/3, 1/3, 1/3) чтобы не получить деление на 0.
        - Constant series → quantile thresholds равны, все returns
          оказываются в FLAT → матрица 0,1,0 со стационарным π=(0,1,0).
    """
    n = len(returns)
    if n < MIN_BARS_FOR_MARKOV:
        return None

    sorted_r = sorted(returns)
    q_low = _quantile(sorted_r, quantiles[0])
    q_high = _quantile(sorted_r, quantiles[1])
    if q_low > q_high:
        q_low, q_high = q_high, q_low

    states = _quantize(returns, q_low, q_high)
    if len(states) < 2:
        return None

    # Эмпирическая матрица переходов 3x3
    counts = [[0, 0, 0], [0, 0, 0], [0, 0, 0]]
    for i in range(len(states) - 1):
        counts[states[i]][states[i + 1]] += 1

    P: List[List[float]] = [[0.0, 0.0, 0.0] for _ in range(3)]
    for i in range(3):
        row_sum = sum(counts[i])
        if row_sum == 0:
            # Состояние не наблюдалось вообще — равномерный prior.
            P[i] = [1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0]
        else:
            P[i] = [counts[i][j] / row_sum for j in range(3)]

    pi = _stationary_distribution(P)
    dwell = {}
    for i, name in enumerate(STATE_NAMES):
        p_stay = P[i][i]
        if p_stay >= 0.999:
            dwell[name] = float("inf")
        else:
            dwell[name] = 1.0 / max(1e-9, (1.0 - p_stay))

    current = states[-1]
    next_step = {name: P[current][i] for i, name in enumerate(STATE_NAMES)}

    return MarkovRegimeResult(
        current_state=STATE_NAMES[current],
        transition_matrix=P,
        stationary_distribution={
            name: pi[i] for i, name in enumerate(STATE_NAMES)
        },
        expected_dwell_bars=dwell,
        next_step_probs=next_step,
        quantile_thresholds=(q_low, q_high),
    )
