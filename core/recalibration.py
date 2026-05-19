"""Isotonic recalibration — Pool-Adjacent-Violators (PAV) algorithm.

Зачем: после PR #2 мы умеем мерить калибровку (Brier, reliability diagram).
PR #2 показал что когда `signal_scorer` говорит «уверенность 70%», на закрытых
сделках realized hit-rate бывает 40–50% — модель overconfident. Этот модуль
делает следующий шаг: **применяет** калибровочную кривую к raw score, чтобы
70% confidence начало реально означать ~70% hit-rate.

Алгоритм PAV (Pool Adjacent Violators):
  1. Сортируем точки (predicted_p, win) по predicted_p.
  2. Каждый y становится своим блоком (mean=y, weight=1).
  3. Идём слева направо, если соседние блоки нарушают монотонность
     (left.mean > right.mean), сливаем их в один с weighted mean.
  4. Повторяем пока не останется нарушений.
  5. Получаем кусочно-постоянную монотонно неубывающую функцию.

Применение к новому p:
  * p ≤ min(x_train) → calibrated = первый блок
  * p ≥ max(x_train) → calibrated = последний блок
  * Иначе бинарный поиск bin'а, возвращаем mean этого блока

Что НЕ делает (намеренно):
  * Не использует numpy/scipy/sklearn (только stdlib).
  * Не подменяет signal_scorer (вызывается отдельно за фичефлагом).
  * Не делает walk-forward (это в `core/walk_forward.py`).
  * Не учитывает временной декей (uniform weight по умолчанию).

Источник правды: `decision_provenance` (PR #1) ⨝ `predictions` через
`link_provenance_outcomes` из PR #2.
"""

from __future__ import annotations

import json
import logging
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

# Минимальное число resolved-точек чтобы фит был осмысленным.
MIN_FIT_POINTS = 20


# ─── Utilities (Brier, hit-rate, score-to-probability) ───────────────────────


def score_to_probability(score: Optional[float]) -> float:
    """Конвертирует raw score [0, 100] в probability [0.5, 1.0].

    Match для `core.calibration._score_to_probability` — score=50 → p=0.50,
    score=100 → p=1.00. Score ниже 50 не должен генерировать сделок,
    но защищаемся clip'ом.
    """
    if score is None:
        return 0.5
    s = max(0.0, min(100.0, float(score)))
    p = s / 100.0
    return max(0.5, p)


def brier_score(probabilities: Sequence[float], wins: Sequence[bool]) -> float:
    """Mean Brier score: average of (p - y)^2.

    Brier ∈ [0, 1], lower is better. 0.25 = подбрасывание монеты,
    0.20 = реально откалибровано, < 0.10 = очень хорошо.
    """
    if not probabilities or len(probabilities) != len(wins):
        raise ValueError("probabilities and wins must be non-empty same length")
    total = 0.0
    for p, w in zip(probabilities, wins):
        y = 1.0 if w else 0.0
        total += (float(p) - y) ** 2
    return total / len(probabilities)


def hit_rate(probabilities: Sequence[float], wins: Sequence[bool], threshold: float = 0.5) -> float:
    """Доля случаев где сделка по probability > threshold действительно win.

    NB: это не accuracy всей модели, а conditional hit-rate среди сделок
    которые модель выдала как «уверенные». Что нам и нужно.
    """
    if not probabilities or len(probabilities) != len(wins):
        raise ValueError("probabilities and wins must be non-empty same length")
    fired = [(p, w) for p, w in zip(probabilities, wins) if p > threshold]
    if not fired:
        return 0.0
    return sum(1 for _, w in fired if w) / len(fired)


# ─── PAV isotonic regression ─────────────────────────────────────────────────


class IsotonicCalibrator:
    """Кусочно-постоянная монотонно неубывающая калибровочная функция.

    Attributes
    ----------
    breakpoints : list[float]
        Отсортированные x-границы (predicted_p) блоков PAV.
    levels : list[float]
        Соответствующее значение калибровки в каждом блоке (∈ [0, 1]).
        len(levels) == len(breakpoints).
    n_train : int
        Число точек обучения.
    """

    def __init__(self) -> None:
        self.breakpoints: list[float] = []
        self.levels: list[float] = []
        self.n_train: int = 0

    def fit(
        self,
        predicted: Sequence[float],
        wins: Sequence[bool],
    ) -> "IsotonicCalibrator":
        """Обучает PAV-isotonic регрессию на парах (predicted_p, win).

        Parameters
        ----------
        predicted : sequence of float in [0, 1]
            Прогнозные вероятности модели.
        wins : sequence of bool
            Реализованные исходы (True = win, False = loss).

        Returns
        -------
        self : IsotonicCalibrator
            Для цепочечного вызова.

        Raises
        ------
        ValueError
            Если входы пустые или разной длины.
        """
        n = len(predicted)
        if n == 0:
            raise ValueError("predicted must be non-empty")
        if n != len(wins):
            raise ValueError("predicted and wins must have same length")

        # Sort by predicted_p ASC. При ties — кладём WINS первыми, LOSSES
        # последними. Так PAV-mergeing увидит нарушение (win=1 потом loss=0)
        # и сольёт их в один блок (общий mean = realized rate этой группы).
        # Без этого «обмана» равные x остаются как два отдельных монотонно-
        # неубывающих блока [0, 1] и калибратор отвечает 0 на x=tied —
        # это ровно та переоценка которой мы хотим избежать.
        pairs = sorted(
            zip(predicted, wins),
            key=lambda pw: (float(pw[0]), 0 if pw[1] else 1),
        )

        # Stack-based PAV: каждая точка приходит как блок weight=1, потом
        # пока top нарушает с предыдущим — мерджим.
        # Block schema: {"mean": float, "weight": int, "x_max": float}
        stack: list[dict] = []
        for p, w in pairs:
            block = {"mean": 1.0 if w else 0.0, "weight": 1, "x_max": float(p)}
            stack.append(block)
            while len(stack) >= 2 and stack[-2]["mean"] > stack[-1]["mean"]:
                top = stack.pop()
                prev = stack.pop()
                merged_weight = prev["weight"] + top["weight"]
                merged_mean = (
                    prev["mean"] * prev["weight"] + top["mean"] * top["weight"]
                ) / merged_weight
                stack.append(
                    {
                        "mean": merged_mean,
                        "weight": merged_weight,
                        # x_max берём с TOP — это правая граница объединённого блока.
                        "x_max": top["x_max"],
                    }
                )

        # Развернём stack в монотонные breakpoints/levels.
        # breakpoints[i] — это правая граница i-го блока (x_max).
        # При predict(p) ищем первый блок где p <= breakpoint → возвращаем его level.
        self.breakpoints = [blk["x_max"] for blk in stack]
        self.levels = [blk["mean"] for blk in stack]
        self.n_train = n
        return self

    def predict(self, predicted_p: float) -> float:
        """Применяет калибровку к новому p.

        Clamp в [0, 1]. Если калибратор не обучен — возвращает raw p.
        """
        if not self.breakpoints:
            # Defensive: ничего не подменяем если калибратор пустой.
            return max(0.0, min(1.0, float(predicted_p)))

        p = max(0.0, min(1.0, float(predicted_p)))

        # Binary search: первый breakpoint >= p.
        lo, hi = 0, len(self.breakpoints) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self.breakpoints[mid] < p:
                lo = mid + 1
            else:
                hi = mid
        # На lo указывает блок где p должен быть; если p больше последнего
        # breakpoint — clamp на последний level.
        if p > self.breakpoints[-1]:
            return self.levels[-1]
        return self.levels[lo]

    def predict_many(self, predicted_ps: Sequence[float]) -> list[float]:
        """Vectorised wrapper над predict()."""
        return [self.predict(p) for p in predicted_ps]

    # ─── Сериализация (для сохранения между запусками) ─────────────────────

    def to_dict(self) -> dict:
        """Сериализует калибратор в JSON-совместимый dict."""
        return {
            "breakpoints": list(self.breakpoints),
            "levels": list(self.levels),
            "n_train": int(self.n_train),
        }

    def to_json(self) -> str:
        """Сериализует в JSON-строку."""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "IsotonicCalibrator":
        """Восстанавливает калибратор из dict."""
        cal = cls()
        cal.breakpoints = [float(x) for x in data.get("breakpoints", [])]
        cal.levels = [float(x) for x in data.get("levels", [])]
        cal.n_train = int(data.get("n_train", 0))
        if len(cal.breakpoints) != len(cal.levels):
            raise ValueError(
                "Inconsistent calibrator: breakpoints and levels length mismatch"
            )
        return cal

    @classmethod
    def from_json(cls, raw: str) -> "IsotonicCalibrator":
        """Восстанавливает из JSON-строки."""
        return cls.from_dict(json.loads(raw))


# ─── Convenience: fit + evaluate в одном вызове ──────────────────────────────


def fit_isotonic_and_evaluate(
    predicted: Sequence[float],
    wins: Sequence[bool],
) -> dict:
    """Фитит PAV на (predicted, wins) и считает раскладку IS-метрик.

    Returns
    -------
    dict с полями:
      * `calibrator` — обученный IsotonicCalibrator,
      * `raw_brier` — Brier на сыром predicted (до калибровки),
      * `calibrated_brier` — Brier на recalibrated predict() (после),
      * `improvement` — `raw_brier - calibrated_brier` (>0 = калибровка помогла),
      * `n` — число точек.

    NB: эти метрики — IN-SAMPLE (оптимистичны). Реальную оценку даёт
    walk-forward в `core/walk_forward.py`.
    """
    if len(predicted) == 0:
        return {
            "calibrator": IsotonicCalibrator(),
            "raw_brier": None,
            "calibrated_brier": None,
            "improvement": None,
            "n": 0,
        }

    cal = IsotonicCalibrator().fit(predicted, wins)
    raw_brier = brier_score(predicted, wins)
    recalibrated = cal.predict_many(predicted)
    cal_brier = brier_score(recalibrated, wins)

    return {
        "calibrator": cal,
        "raw_brier": raw_brier,
        "calibrated_brier": cal_brier,
        "improvement": raw_brier - cal_brier,
        "n": len(predicted),
    }
