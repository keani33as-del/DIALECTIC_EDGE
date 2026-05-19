"""Per-signal calibration — отвечает на вопрос «какие сигналы реально работают».

Зачем: после PR #1 каждое решение бот «морозит» в decision_provenance.
Этот модуль соединяет каждое решение с его реальным исходом (predictions /
backtest_signals) и считает:

  * Hit-rate — overall, per-asset, per-direction, per-regime, per-decision_type.
  * Brier score — насколько калибрована уверенность (score интерпретируется
    как вероятность направления). Brier ∈ [0, 1], ниже = лучше.
    0.25 = подбрасывание монеты, ≤ 0.20 — реально откалибровано.
  * Reliability diagram — 10 бинов по confidence vs realized hit-rate.
    Идеально: в бине 70-80% → realized 70-80%.
  * Signal attribution — какой компонент score breakdown'а лучше
    коррелирует с реальным исходом (Spearman-like).
  * Concept drift — флаг если Brier недавнего окна > исторического +2σ.

Что НЕ делает (намеренно):
  * Не подменяет signal_scorer (только измеряет).
  * Не делает isotonic recalibration (это PR #3).
  * Не делает walk-forward (это PR #3).
  * Не использует numpy/scipy — только stdlib.

Источник данных: фрезы из `decision_provenance` + закрытые prediction'ы
из `predictions`. Если prediction_id уже привязан — точное матчинг.
Иначе — fuzzy-join по (asset, direction, time-window).
"""

from __future__ import annotations

import json
import logging
import math
from typing import Optional

import aiosqlite

from config import DB_PATH

logger = logging.getLogger(__name__)

# Окно по умолчанию для всех агрегатов.
DEFAULT_WINDOW_DAYS = 30

# Fuzzy-join: если provenance.prediction_id is NULL — ищем prediction в этом окне.
_FUZZY_JOIN_HOURS = 2

# Минимум наблюдений для статистически значимых выводов.
_MIN_OBS_OVERALL = 10
_MIN_OBS_BUCKET = 5


# ─── DATA LINKING (provenance ↔ outcome) ─────────────────────────────────────


async def link_provenance_outcomes(
    window_days: int = DEFAULT_WINDOW_DAYS,
    asset: Optional[str] = None,
    decision_type: Optional[str] = None,
) -> list[dict]:
    """Соединяет provenance с реальными исходами.

    Алгоритм:
      1. Берём все provenance за окно.
      2. Если у записи есть `prediction_id` → точно линкуемся к predictions.
      3. Иначе fuzzy-join: ищем prediction с тем же asset+direction в
         пределах ±_FUZZY_JOIN_HOURS от created_at provenance.
      4. Возвращаем list[dict] вида:
           {
               "provenance": {...полная запись decision_provenance...},
               "outcome": "win" | "loss" | "pending" | "caution" | "expired" | None,
               "pnl_pct": float | None,
               "matched_prediction_id": int | None,
           }
         Если outcome=None — prediction не найден.

    Parameters
    ----------
    window_days : int
        Окно в днях (от datetime('now') -window_days дней до сейчас).
    asset : str, optional
        Фильтр по активу (BTC, ETH, ...).
    decision_type : str, optional
        Фильтр по `signal_scorer` или `pick_best`.

    Returns
    -------
    list of dict
        По одной записи на provenance.
    """
    conditions: list[str] = [
        f"p.created_at >= datetime('now', '-{int(window_days)} days')"
    ]
    params: list = []

    if asset:
        conditions.append("p.asset = ?")
        params.append(asset)
    if decision_type:
        conditions.append("p.decision_type = ?")
        params.append(decision_type)

    where_sql = " AND ".join(conditions)
    query = f"""
        SELECT p.*,
               pr.result        AS pred_result,
               pr.pnl_pct       AS pred_pnl_pct,
               pr.id            AS pred_id_direct
          FROM decision_provenance p
     LEFT JOIN predictions pr ON pr.id = p.prediction_id
         WHERE {where_sql}
      ORDER BY p.id DESC
    """

    rows: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            raw_rows = await cur.fetchall()
            for r in raw_rows:
                rows.append(dict(r))

    out: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        for row in rows:
            outcome = row.get("pred_result")
            pnl = row.get("pred_pnl_pct")
            matched_id = row.get("pred_id_direct")

            if outcome is None:
                # Fuzzy-join: тот же asset/direction в окне.
                fuzzy = await _find_matching_prediction(
                    db,
                    asset=row["asset"],
                    direction=row["direction"],
                    created_at=row["created_at"],
                )
                if fuzzy is not None:
                    outcome = fuzzy["result"]
                    pnl = fuzzy.get("pnl_pct")
                    matched_id = fuzzy["id"]

            out.append({
                "provenance": _decode_provenance_row(row),
                "outcome": outcome,
                "pnl_pct": pnl,
                "matched_prediction_id": matched_id,
            })
    return out


async def _find_matching_prediction(
    db: aiosqlite.Connection,
    asset: str,
    direction: str,
    created_at: str,
) -> Optional[dict]:
    """Fuzzy-join: prediction с тем же asset+direction в окне ±_FUZZY_JOIN_HOURS."""
    direction_norm = _normalize_direction(direction)
    if direction_norm is None:
        return None

    query = """
        SELECT id, result, pnl_pct
          FROM predictions
         WHERE asset = ?
           AND UPPER(direction) = ?
           AND result IN ('win', 'loss', 'caution', 'expired')
           AND ABS((julianday(created_at) - julianday(?)) * 24) <= ?
      ORDER BY ABS((julianday(created_at) - julianday(?)) * 24) ASC
         LIMIT 1
    """
    async with db.execute(
        query,
        (asset, direction_norm, created_at, _FUZZY_JOIN_HOURS, created_at),
    ) as cur:
        row = await cur.fetchone()
        if row is None:
            return None
        return dict(row)


def _normalize_direction(direction: str) -> Optional[str]:
    """Приводит direction к канонической форме LONG/SHORT (или None если NONE)."""
    if not direction:
        return None
    d = direction.upper().strip()
    if "LONG" in d or "BULL" in d or "BUY" in d:
        return "LONG"
    if "SHORT" in d or "BEAR" in d or "SELL" in d:
        return "SHORT"
    return None


def _decode_provenance_row(row: dict) -> dict:
    """Раскрывает JSON-поля в decision_provenance row."""
    decoded = {}
    for k, v in row.items():
        if k in ("pred_result", "pred_pnl_pct", "pred_id_direct"):
            continue
        decoded[k] = v
    for json_key in ("features_json", "weights_json", "signals_json", "regime_json"):
        raw = decoded.get(json_key)
        if isinstance(raw, str):
            try:
                decoded[json_key] = json.loads(raw)
            except Exception:
                pass
    return decoded


# ─── METRICS ─────────────────────────────────────────────────────────────────


def _is_win(outcome: Optional[str]) -> Optional[bool]:
    """Возвращает True/False/None для outcome.

    win → True, loss → False, caution/expired/pending/None → None (исключаем).
    """
    if outcome == "win":
        return True
    if outcome == "loss":
        return False
    return None


def _brier_score(probability: float, win: bool) -> float:
    """Brier score для одного предсказания: (p - y)^2."""
    y = 1.0 if win else 0.0
    return (probability - y) ** 2


def _score_to_probability(score: Optional[float]) -> float:
    """Конвертирует score [0, 100] в probability [0.5, 1.0].

    Score интерпретируется как «уверенность что direction правильный».
    Score=50 → p=0.50 (минимум), score=100 → p=1.00.
    Score ниже 50 не должен генерировать сделок (см. порог),
    но защищаемся clip'ом.
    """
    if score is None:
        return 0.5
    s = max(0.0, min(100.0, float(score)))
    # Linear mapping 0..100 → 0.0..1.0, затем clip снизу до 0.5
    p = s / 100.0
    return max(0.5, p)


def compute_overall_stats(linked: list[dict]) -> dict:
    """Aggregated stats: hit-rate, Brier, count, avg pnl.

    Parameters
    ----------
    linked : list of dict
        Результат `link_provenance_outcomes()`.

    Returns
    -------
    dict
        {
            "n_total": int,
            "n_resolved": int,        # с известным win/loss
            "n_wins": int,
            "n_losses": int,
            "hit_rate": float | None, # None если n_resolved < _MIN_OBS_BUCKET
            "brier_mean": float | None,
            "avg_pnl_pct": float | None,
            "is_reliable": bool,      # n_resolved >= _MIN_OBS_OVERALL
        }
    """
    n_total = len(linked)
    wins = 0
    losses = 0
    brier_sum = 0.0
    pnl_sum = 0.0
    pnl_n = 0

    for entry in linked:
        result = _is_win(entry.get("outcome"))
        if result is None:
            continue
        prov = entry.get("provenance") or {}
        score = prov.get("score")
        p = _score_to_probability(score)
        if result:
            wins += 1
        else:
            losses += 1
        brier_sum += _brier_score(p, result)
        pnl = entry.get("pnl_pct")
        if isinstance(pnl, (int, float)):
            pnl_sum += float(pnl)
            pnl_n += 1

    n_resolved = wins + losses
    hit_rate = (wins / n_resolved) if n_resolved >= _MIN_OBS_BUCKET else None
    brier_mean = (brier_sum / n_resolved) if n_resolved > 0 else None
    avg_pnl = (pnl_sum / pnl_n) if pnl_n > 0 else None

    return {
        "n_total": n_total,
        "n_resolved": n_resolved,
        "n_wins": wins,
        "n_losses": losses,
        "hit_rate": hit_rate,
        "brier_mean": brier_mean,
        "avg_pnl_pct": avg_pnl,
        "is_reliable": n_resolved >= _MIN_OBS_OVERALL,
    }


def compute_breakdown_by(linked: list[dict], key_fn) -> dict[str, dict]:
    """Стратифицирует linked по результату `key_fn(entry)`.

    Parameters
    ----------
    linked : list of dict
        Результат `link_provenance_outcomes()`.
    key_fn : callable(entry) -> str | None
        Функция-ключ. Если возвращает None — запись исключается.

    Returns
    -------
    dict mapping bucket_name → stats (как `compute_overall_stats`).
    """
    buckets: dict[str, list[dict]] = {}
    for entry in linked:
        try:
            k = key_fn(entry)
        except Exception:
            k = None
        if k is None:
            continue
        buckets.setdefault(k, []).append(entry)
    return {k: compute_overall_stats(v) for k, v in buckets.items()}


def breakdown_by_asset(linked: list[dict]) -> dict[str, dict]:
    """Hit-rate / Brier на каждый актив."""
    return compute_breakdown_by(
        linked, lambda e: (e.get("provenance") or {}).get("asset")
    )


def breakdown_by_direction(linked: list[dict]) -> dict[str, dict]:
    """Hit-rate / Brier для LONG vs SHORT отдельно."""
    return compute_breakdown_by(
        linked, lambda e: _normalize_direction(
            (e.get("provenance") or {}).get("direction") or ""
        )
    )


def breakdown_by_decision_type(linked: list[dict]) -> dict[str, dict]:
    """Hit-rate / Brier для signal_scorer vs pick_best."""
    return compute_breakdown_by(
        linked, lambda e: (e.get("provenance") or {}).get("decision_type")
    )


def breakdown_by_regime(linked: list[dict]) -> dict[str, dict]:
    """Hit-rate / Brier по режимам тренда (UPTREND / DOWNTREND / SIDEWAYS)."""
    def regime_key(entry: dict) -> Optional[str]:
        prov = entry.get("provenance") or {}
        regime = prov.get("regime_json")
        if not isinstance(regime, dict):
            return None
        trend = regime.get("trend")
        if not trend:
            return None
        t = str(trend).upper().strip()
        if "UP" in t or "BULL" in t:
            return "UPTREND"
        if "DOWN" in t or "BEAR" in t:
            return "DOWNTREND"
        if "SIDE" in t or "FLAT" in t or "RANGE" in t:
            return "SIDEWAYS"
        return None

    return compute_breakdown_by(linked, regime_key)


# ─── RELIABILITY DIAGRAM ─────────────────────────────────────────────────────


def compute_reliability_diagram(
    linked: list[dict],
    n_bins: int = 10,
) -> list[dict]:
    """Bin'ит forecast probability и сравнивает с realized hit-rate.

    Идеально откалиброванная модель: в бине [70-80%] realized hit-rate
    должен быть 75% ± шум. Если в бине 70-80% realized = 50% — модель
    переоценивает уверенность.

    Returns
    -------
    list of dict, по одной записи на бин:
      {
        "bin": "60-70%",
        "bin_low": 0.60,
        "bin_high": 0.70,
        "n": 12,
        "avg_predicted": 0.65,   # ср. probability в бине
        "actual_hit_rate": 0.58, # сколько реально win
        "calibration_gap": -0.07,
      }
    Бины с n==0 пропускаются.
    """
    n_bins = max(1, int(n_bins))
    bin_width = 1.0 / n_bins

    bins: list[list[dict]] = [[] for _ in range(n_bins)]
    # Эпсилон против floating-point edge'ей (0.7/0.1 = 6.9999…).
    eps = 1e-9
    for entry in linked:
        result = _is_win(entry.get("outcome"))
        if result is None:
            continue
        prov = entry.get("provenance") or {}
        p = _score_to_probability(prov.get("score"))
        idx = min(n_bins - 1, int((p + eps) / bin_width))
        bins[idx].append({"p": p, "win": 1 if result else 0})

    out: list[dict] = []
    for i, bucket in enumerate(bins):
        if not bucket:
            continue
        n = len(bucket)
        avg_p = sum(b["p"] for b in bucket) / n
        actual = sum(b["win"] for b in bucket) / n
        out.append({
            "bin": f"{int(i * bin_width * 100)}-{int((i + 1) * bin_width * 100)}%",
            "bin_low": i * bin_width,
            "bin_high": (i + 1) * bin_width,
            "n": n,
            "avg_predicted": avg_p,
            "actual_hit_rate": actual,
            "calibration_gap": actual - avg_p,
        })
    return out


# ─── SIGNAL ATTRIBUTION ──────────────────────────────────────────────────────


def compute_signal_attribution(linked: list[dict]) -> dict[str, dict]:
    """Per-component correlation: какой сигнал лучше всего предсказывает исход.

    Для каждого ключа в `weights_json` (trend_alignment, complexity_hint,
    tradeable_score, и т.д.) считаем:
      * mean_value_on_wins — средний вклад этого компонента когда сделка
        в плюс.
      * mean_value_on_losses — то же для проигранных.
      * separation — разница (на сколько компонент «отличает» win от loss).
      * n_total — сколько раз компонент присутствовал в resolved decisions.

    Returns
    -------
    dict mapping component_name → stats. Отсортирован по abs(separation) убыв.
    """
    component_values: dict[str, dict[str, list[float]]] = {}

    for entry in linked:
        result = _is_win(entry.get("outcome"))
        if result is None:
            continue
        prov = entry.get("provenance") or {}
        weights = prov.get("weights_json")
        if not isinstance(weights, dict):
            continue
        for k, v in weights.items():
            if not isinstance(v, (int, float)):
                continue
            slot = component_values.setdefault(k, {"wins": [], "losses": []})
            (slot["wins"] if result else slot["losses"]).append(float(v))

    out: dict[str, dict] = {}
    for k, slot in component_values.items():
        wins = slot["wins"]
        losses = slot["losses"]
        n_total = len(wins) + len(losses)
        if n_total < _MIN_OBS_BUCKET:
            continue
        mw = (sum(wins) / len(wins)) if wins else None
        ml = (sum(losses) / len(losses)) if losses else None
        sep = (mw - ml) if (mw is not None and ml is not None) else None
        out[k] = {
            "n_wins": len(wins),
            "n_losses": len(losses),
            "mean_on_wins": mw,
            "mean_on_losses": ml,
            "separation": sep,
        }

    # Сортируем по abs(separation) — самый информативный сигнал сверху.
    sorted_keys = sorted(
        out.keys(),
        key=lambda k: abs(out[k]["separation"] or 0.0),
        reverse=True,
    )
    return {k: out[k] for k in sorted_keys}


# ─── DRIFT DETECTION ─────────────────────────────────────────────────────────


async def detect_concept_drift(
    recent_days: int = 14,
    baseline_days: int = 60,
    sigma_threshold: float = 2.0,
) -> dict:
    """Сравнивает Brier недавнего окна vs историческое окно.

    Drift detected если |recent_brier - baseline_brier| > sigma_threshold * SE.

    Returns
    -------
    dict:
      {
        "recent_n":          int,
        "baseline_n":        int,
        "recent_brier":      float | None,
        "baseline_brier":    float | None,
        "delta":             float | None,
        "standard_error":    float | None,
        "z_score":           float | None,
        "drift_detected":    bool,
        "verdict":           "STABLE" | "DRIFT" | "INSUFFICIENT_DATA",
      }
    """
    recent_linked = await link_provenance_outcomes(window_days=recent_days)
    baseline_linked = await link_provenance_outcomes(window_days=baseline_days)

    recent_stats = compute_overall_stats(recent_linked)
    baseline_stats = compute_overall_stats(baseline_linked)

    rn = recent_stats["n_resolved"]
    bn = baseline_stats["n_resolved"]
    rb = recent_stats["brier_mean"]
    bb = baseline_stats["brier_mean"]

    if rn < _MIN_OBS_BUCKET or bn < _MIN_OBS_OVERALL or rb is None or bb is None:
        return {
            "recent_n": rn,
            "baseline_n": bn,
            "recent_brier": rb,
            "baseline_brier": bb,
            "delta": None,
            "standard_error": None,
            "z_score": None,
            "drift_detected": False,
            "verdict": "INSUFFICIENT_DATA",
        }

    delta = rb - bb
    # SE для Brier ≈ sqrt(variance / n). Используем simple Wilson-ish bound.
    # Variance of Brier under Bernoulli ≈ p(1-p) ≤ 0.25.
    se = math.sqrt(0.25 / rn + 0.25 / bn)
    z = (delta / se) if se > 0 else 0.0
    drift = abs(z) > sigma_threshold

    return {
        "recent_n": rn,
        "baseline_n": bn,
        "recent_brier": rb,
        "baseline_brier": bb,
        "delta": delta,
        "standard_error": se,
        "z_score": z,
        "drift_detected": drift,
        "verdict": "DRIFT" if drift else "STABLE",
    }


# ─── TELEGRAM RENDERING ──────────────────────────────────────────────────────


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.1f}%"


def _fmt_float(v: Optional[float], digits: int = 3) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}"


def _fmt_signed_pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else "−"
    return f"{sign}{abs(v):.2f}%"


def format_overall_telegram(stats: dict, window_days: int) -> str:
    """Базовый блок с общей статистикой."""
    lines = [
        f"📊 *Калибровка за {window_days} дней*",
        "",
        f"Всего решений: {stats['n_total']}",
        f"Закрытых: {stats['n_resolved']} (wins {stats['n_wins']} / losses {stats['n_losses']})",
        f"Hit-rate: *{_fmt_pct(stats['hit_rate'])}*",
        f"Brier: *{_fmt_float(stats['brier_mean'])}* (≤0.20 = калиброван)",
        f"Avg PnL: *{_fmt_signed_pct(stats['avg_pnl_pct'])}*",
    ]
    if not stats["is_reliable"]:
        lines.append("")
        lines.append(f"⚠️ Мало данных (нужно ≥{_MIN_OBS_OVERALL} закрытых).")
    return "\n".join(lines)


def format_breakdown_telegram(
    breakdown: dict[str, dict],
    title: str,
    limit: int = 8,
) -> str:
    """Рендерит словарь bucket → stats в Telegram-таблицу."""
    if not breakdown:
        return f"_{title}: нет данных_"

    # Сортируем по убыванию n_resolved — самые показательные сверху.
    items = sorted(
        breakdown.items(),
        key=lambda kv: kv[1]["n_resolved"],
        reverse=True,
    )[:limit]

    lines = [f"*{title}*"]
    for name, s in items:
        if s["n_resolved"] < _MIN_OBS_BUCKET:
            lines.append(f"  • {name}: n={s['n_resolved']} (мало)")
            continue
        lines.append(
            f"  • {name}: hit *{_fmt_pct(s['hit_rate'])}* "
            f"Brier {_fmt_float(s['brier_mean'])} "
            f"(n={s['n_resolved']})"
        )
    return "\n".join(lines)


def format_reliability_telegram(diagram: list[dict]) -> str:
    """Reliability diagram в Telegram."""
    if not diagram:
        return "_Reliability diagram: нет данных_"

    lines = ["*Reliability diagram*", "_Bin | predicted → actual | gap | n_"]
    for b in diagram:
        gap = b["calibration_gap"]
        gap_sign = "+" if gap >= 0 else "−"
        lines.append(
            f"  {b['bin']}: "
            f"{_fmt_pct(b['avg_predicted'])} → *{_fmt_pct(b['actual_hit_rate'])}* "
            f"({gap_sign}{abs(gap) * 100:.0f}pp, n={b['n']})"
        )
    return "\n".join(lines)


def format_attribution_telegram(
    attribution: dict[str, dict],
    limit: int = 6,
) -> str:
    """Top компонентов по separation."""
    if not attribution:
        return "_Attribution: нет данных_"

    lines = ["*Top компоненты* (separation wins vs losses)"]
    for i, (name, s) in enumerate(attribution.items()):
        if i >= limit:
            break
        sep = s["separation"]
        if sep is None:
            continue
        sign = "+" if sep >= 0 else "−"
        lines.append(
            f"  • {name}: {sign}{abs(sep):.2f} "
            f"(wins μ={_fmt_float(s['mean_on_wins'], 2)}, "
            f"losses μ={_fmt_float(s['mean_on_losses'], 2)})"
        )
    return "\n".join(lines)


def format_drift_telegram(drift: dict) -> str:
    """Concept drift verdict."""
    verdict = drift.get("verdict")
    if verdict == "INSUFFICIENT_DATA":
        return "_Drift: недостаточно данных_"

    emoji = "🚨" if drift["drift_detected"] else "✅"
    lines = [
        f"{emoji} *Concept drift: {verdict}*",
        f"  Recent Brier: {_fmt_float(drift['recent_brier'])} (n={drift['recent_n']})",
        f"  Baseline Brier: {_fmt_float(drift['baseline_brier'])} (n={drift['baseline_n']})",
        f"  Δ = {_fmt_float(drift['delta'])}, z = {_fmt_float(drift['z_score'], 2)}",
    ]
    return "\n".join(lines)
