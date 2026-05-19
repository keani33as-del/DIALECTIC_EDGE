"""Walk-forward backtest harness — честная out-of-sample оценка калибровки.

Зачем: PR #2 (`core.calibration`) меряет калибровку in-sample —
«на всех закрытых сделках за окно». Это даёт первую картину но **систематически
переоценивает** реальную работу модели, потому что in-sample учитывает все
точки сразу.

PR #3 (этот модуль) делает rolling walk-forward:
  1. Берёт все resolved decisions из `decision_provenance` ⨝ `predictions`.
  2. Сортирует по времени.
  3. Делит на N фолдов методом «sliding window»:
       train: дни [t, t+train_days)
       test:  дни [t+train_days, t+train_days+test_days)
       step:  t += step_days
  4. В каждом фолде:
       — фитит `IsotonicCalibrator` на train,
       — применяет к test,
       — считает Brier/hit-rate **отдельно** для raw vs calibrated на test.
  5. Агрегирует OOS-метрики через фолды.

Что это даёт:
  * Честный ответ: «если бы мы калибровали с использованием только данных
    до момента T, насколько калибрация улучшила бы прогнозы за следующие
    test_days дней?» — НЕ глядя в будущее.
  * Verdict `READY` если средняя OOS Brier (calibrated) < raw, иначе
    `RAW_BETTER` или `INSUFFICIENT_DATA`.

Что НЕ делает:
  * Не подменяет signal_scorer (вызывается отдельно за фичефлагом).
  * Не делает Bayesian rolling update — это PR #4.
  * Не делает k-fold или blocking — только time-based rolling.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from statistics import mean
from typing import Optional

from core.calibration import (
    _is_win,
    _normalize_direction,
    link_provenance_outcomes,
)
from core.recalibration import (
    IsotonicCalibrator,
    brier_score,
    hit_rate,
    score_to_probability,
)

logger = logging.getLogger(__name__)

# Дефолты: 30д общее окно, 14д train, 7д test, step 7д.
DEFAULT_TOTAL_DAYS = 30
DEFAULT_TRAIN_DAYS = 14
DEFAULT_TEST_DAYS = 7
DEFAULT_STEP_DAYS = 7

# Минимумы для статистически значимого фолда.
MIN_TRAIN_OBS = 20
MIN_TEST_OBS = 5
# Минимум фолдов для агрегата.
MIN_FOLDS = 2


# ─── Timestamp parsing ───────────────────────────────────────────────────────


def _parse_sqlite_ts(s: str) -> Optional[datetime]:
    """SQLite datetime() возвращает 'YYYY-MM-DD HH:MM:SS'. Python 3.11+
    fromisoformat поддерживает space-delimiter. Возвращает None если
    строка кривая."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def _entry_timestamp(entry: dict) -> Optional[datetime]:
    """Достаёт `created_at` provenance, парсит в datetime."""
    prov = entry.get("provenance") or {}
    return _parse_sqlite_ts(prov.get("created_at") or "")


def _entry_probability(entry: dict) -> float:
    """Превращает score из provenance в probability через
    `score_to_probability`. Match для PR #2 интерпретации."""
    prov = entry.get("provenance") or {}
    return score_to_probability(prov.get("score"))


def _entry_win(entry: dict) -> Optional[bool]:
    """Win=True, Loss=False, иначе None (исключается)."""
    return _is_win(entry.get("outcome"))


# ─── Walk-forward core ───────────────────────────────────────────────────────


def split_resolved_by_time(
    resolved: list[dict],
    train_days: int,
    test_days: int,
    step_days: int,
) -> list[dict]:
    """Делит ОТСОРТИРОВАННЫЕ по времени resolved-точки на фолды.

    Каждый фолд — dict:
      {
        "train_start": datetime,
        "train_end":   datetime,
        "test_end":    datetime,
        "train":       list[entry],
        "test":        list[entry],
      }

    Фильтрация min_train_obs/min_test_obs — на стадии evaluation,
    тут только нарезаем по календарю.
    """
    if not resolved:
        return []

    timestamps = [e["_ts"] for e in resolved]
    t_min = min(timestamps)
    t_max = max(timestamps)

    folds: list[dict] = []
    train_start = t_min
    # Чтобы test точно поместился до t_max, train_start + train+test <= t_max.
    horizon = timedelta(days=train_days + test_days)

    while train_start + horizon <= t_max + timedelta(days=1):
        train_end = train_start + timedelta(days=train_days)
        test_end = train_end + timedelta(days=test_days)

        train_set = [
            e for e in resolved
            if train_start <= e["_ts"] < train_end
        ]
        test_set = [
            e for e in resolved
            if train_end <= e["_ts"] < test_end
        ]

        folds.append({
            "train_start": train_start,
            "train_end": train_end,
            "test_end": test_end,
            "train": train_set,
            "test": test_set,
        })
        train_start += timedelta(days=step_days)

    return folds


def evaluate_fold(fold: dict) -> Optional[dict]:
    """Фитит калибратор на train, оценивает на test, возвращает metrics.

    Возвращает None если данных в фолде недостаточно для значимой
    оценки (n_train < MIN_TRAIN_OBS или n_test < MIN_TEST_OBS).
    """
    train = fold["train"]
    test = fold["test"]

    if len(train) < MIN_TRAIN_OBS or len(test) < MIN_TEST_OBS:
        return None

    train_probs = [_entry_probability(e) for e in train]
    train_wins = [_entry_win(e) for e in train]
    # Защита от None — на случай rare path после filter (не должно быть, но всё же).
    train_pairs = [(p, w) for p, w in zip(train_probs, train_wins) if w is not None]
    if len(train_pairs) < MIN_TRAIN_OBS:
        return None
    train_probs_clean = [p for p, _ in train_pairs]
    train_wins_clean = [w for _, w in train_pairs]

    test_probs = [_entry_probability(e) for e in test]
    test_wins = [_entry_win(e) for e in test]
    test_pairs = [(p, w) for p, w in zip(test_probs, test_wins) if w is not None]
    if len(test_pairs) < MIN_TEST_OBS:
        return None
    test_probs_clean = [p for p, _ in test_pairs]
    test_wins_clean = [w for _, w in test_pairs]

    cal = IsotonicCalibrator().fit(train_probs_clean, train_wins_clean)

    raw_oos_brier = brier_score(test_probs_clean, test_wins_clean)
    cal_predictions = cal.predict_many(test_probs_clean)
    cal_oos_brier = brier_score(cal_predictions, test_wins_clean)

    raw_oos_hitrate = hit_rate(test_probs_clean, test_wins_clean, threshold=0.5)
    cal_oos_hitrate = hit_rate(cal_predictions, test_wins_clean, threshold=0.5)

    return {
        "train_start": fold["train_start"].isoformat(),
        "train_end": fold["train_end"].isoformat(),
        "test_end": fold["test_end"].isoformat(),
        "n_train": len(train_probs_clean),
        "n_test": len(test_probs_clean),
        "raw_oos_brier": raw_oos_brier,
        "calibrated_oos_brier": cal_oos_brier,
        "brier_improvement": raw_oos_brier - cal_oos_brier,
        "raw_oos_hitrate": raw_oos_hitrate,
        "calibrated_oos_hitrate": cal_oos_hitrate,
        "hitrate_improvement": cal_oos_hitrate - raw_oos_hitrate,
    }


def aggregate_folds(fold_metrics: list[dict]) -> dict:
    """Агрегирует OOS-метрики по фолдам.

    Verdict:
      * INSUFFICIENT_DATA — фолдов < MIN_FOLDS
      * CALIBRATION_HELPS — mean(cal_brier) < mean(raw_brier) И
        больше половины фолдов показали улучшение
      * RAW_BETTER — иначе

    Returns
    -------
    dict с агрегатами и verdict'ом.
    """
    if len(fold_metrics) < MIN_FOLDS:
        return {
            "verdict": "INSUFFICIENT_DATA",
            "n_folds": len(fold_metrics),
            "raw_oos_brier_mean": None,
            "calibrated_oos_brier_mean": None,
            "absolute_improvement": None,
            "relative_improvement_pct": None,
            "n_folds_with_improvement": 0,
        }

    raw_briers = [f["raw_oos_brier"] for f in fold_metrics]
    cal_briers = [f["calibrated_oos_brier"] for f in fold_metrics]
    improvements = [f["brier_improvement"] for f in fold_metrics]

    raw_mean = mean(raw_briers)
    cal_mean = mean(cal_briers)
    n_better = sum(1 for d in improvements if d > 0)

    rel_pct = ((raw_mean - cal_mean) / raw_mean * 100.0) if raw_mean > 0 else 0.0

    helps = (cal_mean < raw_mean) and (n_better > len(fold_metrics) / 2)
    verdict = "CALIBRATION_HELPS" if helps else "RAW_BETTER"

    return {
        "verdict": verdict,
        "n_folds": len(fold_metrics),
        "raw_oos_brier_mean": raw_mean,
        "calibrated_oos_brier_mean": cal_mean,
        "absolute_improvement": raw_mean - cal_mean,
        "relative_improvement_pct": rel_pct,
        "n_folds_with_improvement": n_better,
    }


# ─── Top-level entry point ──────────────────────────────────────────────────


async def walk_forward_backtest(
    window_days: int = DEFAULT_TOTAL_DAYS,
    train_days: int = DEFAULT_TRAIN_DAYS,
    test_days: int = DEFAULT_TEST_DAYS,
    step_days: int = DEFAULT_STEP_DAYS,
    asset: Optional[str] = None,
    decision_type: Optional[str] = None,
) -> dict:
    """Один вызов: pull → split → fit → eval → aggregate.

    Parameters
    ----------
    window_days : int
        Всего сколько дней истории брать.
    train_days, test_days, step_days : int
        Параметры sliding-window сплита.
    asset, decision_type : Optional[str]
        Фильтры (как в PR #2).

    Returns
    -------
    dict с:
      * "config" — параметры запуска,
      * "n_resolved" — сколько resolved-точек попало в анализ,
      * "folds" — list of evaluate_fold() результатов,
      * "aggregate" — `aggregate_folds()` верхний уровень,
      * "verdict" — INSUFFICIENT_DATA / CALIBRATION_HELPS / RAW_BETTER.
    """
    linked = await link_provenance_outcomes(
        window_days=window_days,
        asset=asset,
        decision_type=decision_type,
    )

    # Фильтруем resolved + парсим timestamps.
    resolved: list[dict] = []
    for entry in linked:
        win = _entry_win(entry)
        if win is None:
            continue
        ts = _entry_timestamp(entry)
        if ts is None:
            continue
        entry["_ts"] = ts
        # Доп. фильтр на корректность direction (LONG/SHORT).
        prov = entry.get("provenance") or {}
        if _normalize_direction(prov.get("direction") or "") is None:
            continue
        resolved.append(entry)
    resolved.sort(key=lambda e: e["_ts"])

    n_resolved = len(resolved)

    if n_resolved < MIN_TRAIN_OBS + MIN_TEST_OBS:
        return {
            "config": {
                "window_days": window_days,
                "train_days": train_days,
                "test_days": test_days,
                "step_days": step_days,
                "asset": asset,
                "decision_type": decision_type,
            },
            "n_resolved": n_resolved,
            "folds": [],
            "aggregate": {
                "verdict": "INSUFFICIENT_DATA",
                "n_folds": 0,
                "raw_oos_brier_mean": None,
                "calibrated_oos_brier_mean": None,
                "absolute_improvement": None,
                "relative_improvement_pct": None,
                "n_folds_with_improvement": 0,
            },
            "verdict": "INSUFFICIENT_DATA",
        }

    folds_raw = split_resolved_by_time(
        resolved, train_days=train_days, test_days=test_days, step_days=step_days
    )

    fold_metrics: list[dict] = []
    for fold in folds_raw:
        metrics = evaluate_fold(fold)
        if metrics is not None:
            fold_metrics.append(metrics)

    aggregate = aggregate_folds(fold_metrics)

    return {
        "config": {
            "window_days": window_days,
            "train_days": train_days,
            "test_days": test_days,
            "step_days": step_days,
            "asset": asset,
            "decision_type": decision_type,
        },
        "n_resolved": n_resolved,
        "folds": fold_metrics,
        "aggregate": aggregate,
        "verdict": aggregate["verdict"],
    }


# ─── Telegram rendering ──────────────────────────────────────────────────────


def _fmt_pct_signed(v: Optional[float]) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else "−"
    return f"{sign}{abs(v):.2f}pp"


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.1f}%"


def _fmt_float(v: Optional[float], digits: int = 4) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}"


def format_backtest_telegram(result: dict) -> str:
    """Рендерит walk_forward результат в Telegram-сообщение.

    Краткий формат: верхний уровень + список фолдов.
    """
    cfg = result.get("config", {})
    agg = result.get("aggregate", {})
    verdict = result.get("verdict", "INSUFFICIENT_DATA")

    lines: list[str] = []
    lines.append("📈 *Walk-forward backtest*")
    lines.append(
        f"_окно {cfg.get('window_days')}д, train {cfg.get('train_days')}д / "
        f"test {cfg.get('test_days')}д / step {cfg.get('step_days')}д_"
    )
    if cfg.get("asset"):
        lines.append(f"_фильтр: {cfg['asset']}_")

    n_resolved = result.get("n_resolved", 0)
    lines.append("")
    lines.append(f"Resolved решений: *{n_resolved}*")
    lines.append(f"Фолдов: *{agg.get('n_folds', 0)}*")

    if verdict == "INSUFFICIENT_DATA":
        lines.append("")
        lines.append(
            "⚠️ Недостаточно данных. Нужно ≥"
            f"{MIN_TRAIN_OBS + MIN_TEST_OBS} resolved-сделок "
            f"в выбранном окне."
        )
        return "\n".join(lines)

    raw_brier = agg.get("raw_oos_brier_mean")
    cal_brier = agg.get("calibrated_oos_brier_mean")
    rel = agg.get("relative_improvement_pct")

    lines.append("")
    lines.append(f"Raw OOS Brier: *{_fmt_float(raw_brier)}*")
    lines.append(f"Calibrated OOS Brier: *{_fmt_float(cal_brier)}*")
    if raw_brier is not None and cal_brier is not None:
        delta_emoji = "📉" if cal_brier < raw_brier else "📈"
        lines.append(
            f"{delta_emoji} Δ Brier: *{_fmt_float(raw_brier - cal_brier)}* "
            f"({_fmt_pct_signed(rel)} относительно raw)"
        )
    lines.append(
        f"Фолдов с улучшением: *{agg.get('n_folds_with_improvement', 0)} / "
        f"{agg.get('n_folds', 0)}*"
    )

    lines.append("")
    verdict_label = {
        "CALIBRATION_HELPS": "✅ Калибровка помогает (можно включать в проде)",
        "RAW_BETTER": "⚠️ Калибровка не улучшает (рано включать)",
    }.get(verdict, verdict)
    lines.append(f"*Verdict:* {verdict_label}")

    return "\n".join(lines)


def format_backtest_folds_telegram(result: dict, limit: int = 5) -> str:
    """Детальный список последних N фолдов (по убыванию даты)."""
    folds = result.get("folds", [])
    if not folds:
        return "_Фолды: нет данных_"

    sorted_folds = sorted(
        folds, key=lambda f: f.get("test_end", ""), reverse=True
    )[:limit]

    lines = ["*Последние фолды*"]
    for f in sorted_folds:
        ts = f.get("test_end", "")[:10]  # YYYY-MM-DD
        delta_b = f.get("brier_improvement", 0.0)
        sign_b = "+" if delta_b > 0 else "−"
        delta_h = f.get("hitrate_improvement", 0.0)
        sign_h = "+" if delta_h > 0 else "−"
        lines.append(
            f"  • test до {ts}: "
            f"n_train {f['n_train']}, n_test {f['n_test']}, "
            f"ΔBrier {sign_b}{abs(delta_b):.4f}, "
            f"Δhit {sign_h}{abs(delta_h) * 100:.0f}pp"
        )
    return "\n".join(lines)
