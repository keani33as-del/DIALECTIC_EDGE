"""
quant_filter.py — Deterministic mean-reversion + regime-gating verdict.

Standalone module that consumes a list of close prices for an asset (and
optionally for BTC as the macro-crypto reference) and produces a directional
verdict (LONG/SHORT/NEUTRAL) with confidence and human-readable reasoning.

Origin
------
Built from an offline backtest (5 crypto × 365 daily bars, walk-forward
90d/30d). See ``docs/quant_research_v2.md`` for the full study. Among 20
candidate rules, the ``v2_combo_final`` ensemble was the best balance of
hit-rate and stability:

    | Strategy              | overall hit | walk-forward robust | N    |
    | --------------------- | ----------- | ------------------- | ---- |
    | baseline MA50/200     | 49.6%       | 39.8                | 2088 |
    | v2_combo_final (this) | 65.9%       | 62.2                |  276 |
    | v2_high_conv          | 75.0%       | 72.1                |  160 |

The bot's previous deterministic signal was the plain MA50/200 trigger
(``build_signal_bias_map`` in ``signals.py``) — ~50%. Replacing it with this
filter lifts directional accuracy without overfitting to the last quarter
(test-set 69.6%, train-set 61.7%, see ``v2_ensemble.py``).

What it does
------------
1) ``BB_revert`` — Bollinger Bands (20, 2σ): fade extremes.
2) ``Donchian_revert`` — fade 20-day high/low breaks.
3) ``RSI_revert`` — fade RSI > 70 / < 30 .
4) Require **at least 2 of 3** to agree (and no opposite vote).
5) **BTC trend gate**: if the asset's mean-reversion vote contradicts BTC's
   own strong trend (price vs MA50/MA200), demote to NEUTRAL.

The default direction is **NEUTRAL**: only when the three indicators
strongly agree we step out of cash. This is by design — the goal is fewer,
higher-quality directional calls (~15% of bars instead of ~50%).

Pure module
-----------
No I/O, no third-party deps. Inputs are plain ``list[float]`` of historical
closes (oldest → newest, the last entry is the bar we're judging). Returns
a dict with the verdict, components, and a short Russian reason string for
display in ``/markets`` and ``/daily``.
"""

from __future__ import annotations

from dataclasses import dataclass


# Constants ────────────────────────────────────────────────────────────────────

MA_FAST = 50
MA_SLOW = 200
RSI_PERIOD = 14
BB_PERIOD = 20
BB_STD_K = 2.0
DONCHIAN_PERIOD = 20

# Thresholds — tuned on the walk-forward backtest (see docs/quant_research_v2.md).
RSI_LOW = 30.0
RSI_HIGH = 70.0
BB_POS_LOW = 0.10   # close - lower_band / band_width
BB_POS_HIGH = 0.90

MIN_HISTORY_FOR_SIGNAL = 60  # need at least RSI+BB warmed up
MIN_HISTORY_FOR_TREND = MA_SLOW + 5  # to compute MA200 meaningfully


# Components ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QuantFeatures:
    """Per-bar derived features. Any field can be ``None`` if there isn't
    enough history to compute it (do not crash on short series).
    """
    close: float
    ma50: float | None
    ma200: float | None
    rsi14: float | None
    bb_pos: float | None
    donch_high_break: bool
    donch_low_break: bool


def _ma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def _rsi(closes: list[float], period: int = RSI_PERIOD) -> float | None:
    """Standard Wilder RSI on daily closes. Returns None if not enough history."""
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, period + 1):
        diff = closes[-(period + 1) + i] - closes[-(period + 1) + i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    # Smooth with the remaining bars (Wilder).
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        avg_g = (avg_g * (period - 1) + gain) / period
        avg_l = (avg_l * (period - 1) + loss) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def _bb_position(closes: list[float], period: int = BB_PERIOD, k: float = BB_STD_K) -> float | None:
    """Normalised position of last close inside Bollinger Bands.
    Returns 0.0 at lower band, 1.0 at upper band. Clamped at edges.
    """
    if len(closes) < period:
        return None
    window = closes[-period:]
    mean = sum(window) / period
    var = sum((x - mean) ** 2 for x in window) / period
    sigma = var ** 0.5
    if sigma <= 0:
        return 0.5
    upper = mean + k * sigma
    lower = mean - k * sigma
    band_width = upper - lower
    if band_width <= 0:
        return 0.5
    return (closes[-1] - lower) / band_width


def _donchian_break(closes: list[float], period: int = DONCHIAN_PERIOD) -> tuple[bool, bool]:
    """Returns ``(high_break, low_break)``.
    The current close is compared against the previous N closes (excluding
    itself) so a break means we just punched through the prior range.
    """
    if len(closes) < period + 1:
        return False, False
    window = closes[-(period + 1):-1]
    last = closes[-1]
    hi = max(window)
    lo = min(window)
    return last > hi, last < lo


def build_features(closes: list[float]) -> QuantFeatures:
    """Compute all features needed by the verdict from a price history."""
    last_close = closes[-1] if closes else 0.0
    hi, lo = _donchian_break(closes)
    return QuantFeatures(
        close=last_close,
        ma50=_ma(closes, MA_FAST),
        ma200=_ma(closes, MA_SLOW),
        rsi14=_rsi(closes),
        bb_pos=_bb_position(closes),
        donch_high_break=hi,
        donch_low_break=lo,
    )


# Individual mean-reversion components ────────────────────────────────────────


def _bb_vote(f: QuantFeatures) -> str:
    if f.bb_pos is None:
        return "NEUTRAL"
    if f.bb_pos >= BB_POS_HIGH:
        return "SHORT"
    if f.bb_pos <= BB_POS_LOW:
        return "LONG"
    return "NEUTRAL"


def _donchian_vote(f: QuantFeatures) -> str:
    """Fade the breakout (mean-revert): a fresh 20-day high → SHORT, a fresh
    20-day low → LONG. This is the inverse of trend-following Donchian.
    """
    if f.donch_high_break:
        return "SHORT"
    if f.donch_low_break:
        return "LONG"
    return "NEUTRAL"


def _rsi_vote(f: QuantFeatures) -> str:
    if f.rsi14 is None:
        return "NEUTRAL"
    if f.rsi14 >= RSI_HIGH:
        return "SHORT"
    if f.rsi14 <= RSI_LOW:
        return "LONG"
    return "NEUTRAL"


def _btc_trend(btc_f: QuantFeatures | None) -> str:
    """Coarse BTC regime: price relative to both MA50 and MA200."""
    if btc_f is None or btc_f.ma50 is None or btc_f.ma200 is None:
        return "NEUTRAL"
    top = max(btc_f.ma50, btc_f.ma200)
    bot = min(btc_f.ma50, btc_f.ma200)
    if btc_f.close > top:
        return "LONG"
    if btc_f.close < bot:
        return "SHORT"
    return "NEUTRAL"


# Top-level verdict ───────────────────────────────────────────────────────────


def quant_verdict(
    closes: list[float],
    btc_closes: list[float] | None = None,
) -> dict:
    """Return a deterministic quant verdict for the asset.

    Args:
        closes: List of daily closes (oldest→newest). Last entry is judged.
        btc_closes: Same for BTC. Optional; if absent, the BTC trend gate is
            disabled (the function still works).

    Returns:
        dict with keys:
            verdict: "LONG" | "SHORT" | "NEUTRAL"
            confidence: 0..100 (heuristic)
            reason: short Russian string for display
            components: { bb, donchian, rsi, btc_trend }
            features: QuantFeatures (as dict)
            status: "ok" | "insufficient_history"
    """
    if not closes or len(closes) < MIN_HISTORY_FOR_SIGNAL:
        return {
            "verdict": "NEUTRAL",
            "confidence": 0,
            "reason": "недостаточно истории для расчёта",
            "components": {},
            "features": None,
            "status": "insufficient_history",
        }

    f = build_features(closes)
    btc_f = build_features(btc_closes) if btc_closes and len(btc_closes) >= MIN_HISTORY_FOR_SIGNAL else None

    bb = _bb_vote(f)
    dc = _donchian_vote(f)
    rs = _rsi_vote(f)
    btc_dir = _btc_trend(btc_f)

    longs = sum(1 for v in (bb, dc, rs) if v == "LONG")
    shorts = sum(1 for v in (bb, dc, rs) if v == "SHORT")

    own = "NEUTRAL"
    if longs >= 2 and shorts == 0:
        own = "LONG"
    elif shorts >= 2 and longs == 0:
        own = "SHORT"

    # BTC regime gate: don't take a counter-rally against a strongly trending BTC
    gated = own
    if own == "SHORT" and btc_dir == "LONG":
        gated = "NEUTRAL"
    elif own == "LONG" and btc_dir == "SHORT":
        gated = "NEUTRAL"

    # Confidence heuristic: 3-of-3 vote = 90, 2-of-3 = 70, gated = 30, neutral = 0.
    if gated == "NEUTRAL" and own != "NEUTRAL":
        confidence = 30
    elif gated == "NEUTRAL":
        confidence = 0
    else:
        all_three_agree = (longs == 3) or (shorts == 3)
        confidence = 90 if all_three_agree else 70

    reason_parts: list[str] = []
    if gated == "NEUTRAL" and own != "NEUTRAL":
        reason_parts.append(f"свой сигнал {own} заблокирован BTC-режимом ({btc_dir})")
    elif gated != "NEUTRAL":
        votes = []
        if bb == gated:
            votes.append("BB" + ("≥0.9" if gated == "SHORT" else "≤0.1"))
        if dc == gated:
            votes.append("Donchian-20 пробой" + (" вверх" if gated == "SHORT" else " вниз"))
        if rs == gated:
            votes.append("RSI" + (">70" if gated == "SHORT" else "<30"))
        reason_parts.append(f"{gated} mean-rev: " + ", ".join(votes))
        if btc_dir == gated:
            reason_parts.append(f"BTC {btc_dir} подтверждает")
        elif btc_dir == "NEUTRAL":
            reason_parts.append("BTC в чопе")
    else:
        reason_parts.append("без конфлюэнции (NEUTRAL)")

    return {
        "verdict": gated,
        "confidence": confidence,
        "reason": "; ".join(reason_parts),
        "components": {
            "bb": bb,
            "donchian": dc,
            "rsi": rs,
            "btc_trend": btc_dir,
            "votes_long": longs,
            "votes_short": shorts,
        },
        "features": {
            "close": f.close,
            "ma50": f.ma50,
            "ma200": f.ma200,
            "rsi14": f.rsi14,
            "bb_pos": f.bb_pos,
            "donch_high_break": f.donch_high_break,
            "donch_low_break": f.donch_low_break,
        },
        "status": "ok",
    }


def quant_verdict_label(verdict: str) -> tuple[str, str]:
    """(emoji, human label) for the verdict, for /markets and /daily display."""
    if verdict == "LONG":
        return "🟢", "Quant: LONG"
    if verdict == "SHORT":
        return "🔴", "Quant: SHORT"
    return "⚪️", "Quant: NEUTRAL"


def reconcile_with_llm(llm_verdict: str, quant_verdict_str: str) -> tuple[str, str]:
    """Reconcile an LLM-produced verdict with the deterministic quant one.

    Rules:
      - If both agree → keep LLM verdict, confidence ↑.
      - If LLM = NEUTRAL → keep NEUTRAL (LLM is conservative by design).
      - If quant = NEUTRAL → keep LLM verdict (LLM may have non-price info).
      - If they strongly disagree (LLM LONG vs quant SHORT, or vice versa) →
        downgrade to NEUTRAL with a note. This is the main safety: keep the
        bot from acting on an LLM hallucination that contradicts price action.

    Returns:
        (final_verdict, note) where note is a short Russian string
        explaining the reconciliation (empty if no override happened).
    """
    llm = (llm_verdict or "").upper()
    quant = (quant_verdict_str or "").upper()
    # Normalise LLM to LONG/SHORT/NEUTRAL space
    if llm in ("BUY", "BULL", "BULLISH"):
        llm = "LONG"
    elif llm in ("SELL", "BEAR", "BEARISH"):
        llm = "SHORT"
    elif llm not in ("LONG", "SHORT", "NEUTRAL"):
        llm = "NEUTRAL"

    if quant not in ("LONG", "SHORT", "NEUTRAL"):
        quant = "NEUTRAL"

    if llm == quant and llm != "NEUTRAL":
        return llm, f"LLM и quant согласны: {llm}"
    if quant == "NEUTRAL" or llm == "NEUTRAL":
        return llm, ""
    if llm != quant:
        return "NEUTRAL", f"конфликт LLM ({llm}) vs quant ({quant}) → NEUTRAL"
    return llm, ""
