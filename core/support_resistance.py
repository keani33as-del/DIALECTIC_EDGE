# -*- coding: utf-8 -*-
"""Support/Resistance level detection from OHLC.

Алгоритм (D1):
  1. **Pivot-points** — локальные high/low с lookback ±N (по умолчанию 5).
     Локальный high — бар чей high > всех соседних high'ов в окне ±N.
     Локальный low — симметрично. Это даёт "сырые" точки разворота.

  2. **Clustering** — соседние уровни (в пределах `tolerance_pct` % друг от
     друга) сливаются в один **кластер**. Это превращает 3 близких касания
     ($81,950 / $81,960 / $82,010) в один уровень $81,973 с touches=3.

  3. **Strength scoring** — каждый кластер получает score:
        score = touches * 1.0 + recency_bonus
     где recency_bonus = exp(-Δбаров / 30) — недавние касания весят больше.

  4. **Filter near current price** — выбираем top-N кластеров **выше** цены
     (resistances) и top-N **ниже** (supports). Если их меньше N — отдаём
     что есть.

Используется в `/markets` для рендера 2 уровней сопротивления + 2 поддержки
на каждый актив. Не используется в торговой логике (`signal_trader.py`),
только информационно.

Сложность: O(N × lookback) для pivot detection, O(K²) для clustering где K
— число pivots. На 250 баров с lookback=5 → ~50 pivots → ~2.5k операций.
Время ~1-2 мс на актив. Дёшево.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


PivotType = Literal["HIGH", "LOW"]


@dataclass(frozen=True)
class Pivot:
    """Локальный экстремум (swing high/low)."""

    idx: int  # индекс в OHLC массиве (0-based)
    price: float
    kind: PivotType


@dataclass(frozen=True)
class Level:
    """Кластер уровней — финальный S/R-уровень после clustering."""

    price: float  # средняя цена кластера
    touches: int  # сколько пивотов попало в кластер
    last_idx: int  # индекс последнего касания (для recency)
    kind: PivotType  # HIGH = resistance, LOW = support
    score: float  # strength score (touches + recency bonus)
    bars_ago: int = 0  # сколько баров назад было последнее касание (для рендера «свинг-Nд»)


@dataclass(frozen=True)
class SRLevels:
    """Итог `compute_sr_levels`: top-N выше и ниже текущей цены."""

    resistances: list[Level]  # выше цены, отсортированы по proximity (ближайший первым)
    supports: list[Level]  # ниже цены, отсортированы по proximity (ближайший первым)


def find_pivot_points(
    highs: list[float],
    lows: list[float],
    *,
    lookback: int = 5,
) -> list[Pivot]:
    """Возвращает список локальных пиков и впадин.

    Пик в индексе `i` — это бар чей `high` строго больше всех high'ов в окне
    `[i-lookback, i-1]` И больше-или-равен всех high'ов в окне `[i+1, i+lookback]`.
    Симметрично для впадин (low'ов).

    Граничные бары (первые/последние `lookback` штук) пропускаются — на них
    нельзя надёжно определить пивот.

    Args:
        highs: массив дневных high цен (len >= 2*lookback+1)
        lows:  массив дневных low цен (тот же len что highs)
        lookback: окно поиска ±N баров (по умолчанию 5 = ~неделя на D1)

    Returns:
        Список `Pivot` в хронологическом порядке.
    """
    if len(highs) != len(lows):
        raise ValueError(f"highs/lows length mismatch: {len(highs)} vs {len(lows)}")
    n = len(highs)
    if n < 2 * lookback + 1:
        return []

    pivots: list[Pivot] = []
    for i in range(lookback, n - lookback):
        # Local high: strictly > all left, >= all right (right-biased).
        # Right-biased ">=" чтобы не пропускать плато: [...100, 105, 105, 100] —
        # оба 105 валидные пивоты (мы возьмём левый, второй проигнорим из-за `>`).
        h = highs[i]
        is_high = (
            all(h > highs[j] for j in range(i - lookback, i))
            and all(h >= highs[j] for j in range(i + 1, i + lookback + 1))
        )
        if is_high:
            pivots.append(Pivot(idx=i, price=h, kind="HIGH"))
            continue
        l = lows[i]
        is_low = (
            all(l < lows[j] for j in range(i - lookback, i))
            and all(l <= lows[j] for j in range(i + 1, i + lookback + 1))
        )
        if is_low:
            pivots.append(Pivot(idx=i, price=l, kind="LOW"))
    return pivots


def cluster_levels(
    pivots: list[Pivot],
    *,
    tolerance_pct: float = 0.5,
) -> list[Level]:
    """Сливает близкие пивоты одного типа в кластеры.

    Два пивота одного `kind` принадлежат одному кластеру если их цены
    отличаются менее чем на `tolerance_pct` процентов. Кластер
    представлен **средней** ценой всех пивотов внутри.

    Args:
        pivots: результат `find_pivot_points`
        tolerance_pct: процент допуска (по умолчанию 0.5%, т.е. два уровня
                       сливаются если разница < 0.5%)

    Returns:
        Список `Level` отсортированный по цене (по возрастанию).
    """
    if not pivots:
        return []
    if tolerance_pct < 0:
        raise ValueError("tolerance_pct must be non-negative")

    # Sort by price; cluster adjacent ones if within tolerance and same kind.
    sorted_pivots = sorted(pivots, key=lambda p: p.price)
    clusters: list[list[Pivot]] = []
    for p in sorted_pivots:
        if not clusters:
            clusters.append([p])
            continue
        last_cluster = clusters[-1]
        last_p = last_cluster[-1]
        same_kind = last_p.kind == p.kind
        within_tolerance = (
            abs(p.price - last_p.price) / max(last_p.price, 1e-9) * 100
            <= tolerance_pct
        )
        if same_kind and within_tolerance:
            last_cluster.append(p)
        else:
            clusters.append([p])

    levels: list[Level] = []
    for cluster in clusters:
        avg_price = sum(p.price for p in cluster) / len(cluster)
        levels.append(
            Level(
                price=avg_price,
                touches=len(cluster),
                last_idx=max(p.idx for p in cluster),
                kind=cluster[0].kind,
                score=0.0,  # будет заполнено в score_clusters
            )
        )
    return sorted(levels, key=lambda lv: lv.price)


def score_clusters(
    clusters: list[Level],
    *,
    current_idx: int,
    recency_halflife: float = 30.0,
) -> list[Level]:
    """Назначает каждому кластеру score = touches + recency_bonus.

    Recency bonus считается как `exp(-Δбаров / recency_halflife)`, где
    Δбаров = разница между `current_idx` и `last_idx` кластера. Уровень
    касавшийся 5 баров назад получит почти полный bonus (~0.85), а
    касавшийся 100 баров назад — почти ноль (~0.04).

    Args:
        clusters: результат `cluster_levels`
        current_idx: индекс текущего бара в OHLC (обычно `len(closes) - 1`)
        recency_halflife: характерный период «забывания» (баров)

    Returns:
        Новые `Level` с заполненным `score`.
    """
    if recency_halflife <= 0:
        raise ValueError("recency_halflife must be positive")
    out: list[Level] = []
    for lv in clusters:
        delta = max(0, current_idx - lv.last_idx)
        recency = math.exp(-delta / recency_halflife)
        out.append(
            Level(
                price=lv.price,
                touches=lv.touches,
                last_idx=lv.last_idx,
                kind=lv.kind,
                score=float(lv.touches) + recency,
                bars_ago=delta,
            )
        )
    return out


def compute_sr_levels(
    highs: list[float],
    lows: list[float],
    *,
    current_price: float,
    lookback: int = 5,
    tolerance_pct: float = 0.5,
    recency_halflife: float = 30.0,
    num_each_side: int = 2,
) -> SRLevels:
    """End-to-end: highs/lows → top-N supports + top-N resistances.

    Args:
        highs: дневные high'и (рекомендуется 100-250 баров)
        lows:  дневные low'и (тот же len)
        current_price: текущая цена (фильтрация по сторонам)
        lookback: окно для pivot-detection
        tolerance_pct: допуск для clustering (в %)
        recency_halflife: характерный период забывания (баров)
        num_each_side: сколько уровней брать с каждой стороны

    Returns:
        `SRLevels` с supports/resistances отсортированными по близости
        к `current_price` (ближайший первым).

    Если данных мало (< 2*lookback+1 баров) — возвращает пустой SRLevels.
    """
    pivots = find_pivot_points(highs, lows, lookback=lookback)
    if not pivots:
        return SRLevels(resistances=[], supports=[])

    clusters = cluster_levels(pivots, tolerance_pct=tolerance_pct)
    current_idx = len(highs) - 1
    scored = score_clusters(clusters, current_idx=current_idx, recency_halflife=recency_halflife)

    # Разделяем по сторонам относительно текущей цены.
    # NB: HIGH-пивот может оказаться НИЖЕ текущей цены если рынок ушёл выше
    # старого сопротивления — теперь это support (broken resistance becomes
    # support). Мы это игнорируем: классифицируем по СТОРОНЕ от цены, а не по
    # kind. Это даёт более интуитивный UX в /markets.
    above = [lv for lv in scored if lv.price > current_price]
    below = [lv for lv in scored if lv.price < current_price]

    # Сортируем: сначала по score (сильные сверху), затем берём top-N,
    # потом пере-сортировываем по proximity (ближайший к цене первым).
    above_top = sorted(above, key=lambda lv: -lv.score)[:num_each_side]
    below_top = sorted(below, key=lambda lv: -lv.score)[:num_each_side]

    resistances = sorted(above_top, key=lambda lv: lv.price)  # ascending: R₁ ближе, R₂ дальше
    supports = sorted(below_top, key=lambda lv: -lv.price)  # descending: S₁ ближе, S₂ дальше

    return SRLevels(resistances=resistances, supports=supports)


def label_level_source(level: Level, *, ma50: float | None = None, ma200: float | None = None) -> str:
    """Подпись для S/R-уровня: либо «MA50» / «MA200» если совпадает, либо
    «свинг-Nд» (сколько баров назад было касание).

    Args:
        level: оцениваемый уровень
        ma50: текущее значение MA50 (опционально, для confluence-метки)
        ma200: текущее значение MA200 (опционально)

    Returns:
        Короткая строка для рендера, напр. `"MA200"`, `"свинг-15д"`.
    """
    # Confluence с MA — если цена кластера в пределах 0.5% от MA, пишем MA.
    for ma_val, ma_name in ((ma50, "MA50"), (ma200, "MA200")):
        if ma_val is None or ma_val <= 0:
            continue
        if abs(level.price - ma_val) / ma_val * 100 <= 0.5:
            return ma_name
    # Иначе — свинг с указанием давности.
    if level.bars_ago > 0:
        return f"свинг-{level.bars_ago}д"
    return "свинг"
