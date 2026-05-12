"""
Horizon-pack: parametric bundle that drives stop/target sizing, R/R minimum,
position size caps and prompt overlays for different planning horizons.

ONE knob — `horizon` (intraday | swing | position) — flows from `/daily` UI
through analysis_service into Synth so that all three agents (Bull/Bear/Synth)
write a plan that is internally consistent with the requested timeframe.

Without this module Bull/Bear/Synth used hardcoded swing parameters
(stop ≈ 2.5×ATR, R/R ≥ 1:2, size up to 15%). For an intraday plan that's
absurdly wide; for a position plan it's far too tight. Now every parameter
lives in one place and is injected into Synth's prompt + the deterministic
plan renderer + the cache key.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class HorizonPack:
    key: str
    label: str
    candle_tf: str
    candles_back: int
    news_window_h: int
    stop_atr_mult: float
    stop_pct_crypto: float
    stop_pct_stocks: float
    min_rr: float
    size_caps: dict = field(default_factory=dict)
    description: str = ""

    @property
    def label_pretty(self) -> str:
        emoji = {"intraday": "\u26a1", "swing": "\U0001f4c8", "position": "\U0001f3d4"}.get(self.key, "\U0001f3af")
        return f"{emoji} {self.label}"


INTRADAY = HorizonPack(
    key="intraday",
    label="1-3 \u0434\u043d\u044f",
    candle_tf="4h",
    candles_back=30,
    news_window_h=24,
    stop_atr_mult=1.5,
    stop_pct_crypto=0.02,
    stop_pct_stocks=0.01,
    min_rr=1.5,
    size_caps={"risk_on": 0.05, "neutral": 0.03, "risk_off": 0.01},
    description="\u0421\u043a\u0430\u043b\u044c\u043f / \u043a\u0440\u0430\u0442\u043a\u043e\u0441\u0440\u043e\u0447\u043d\u044b\u0435 \u0442\u0440\u0438\u0433\u0433\u0435\u0440\u044b. \u0421\u0442\u043e\u043f\u044b \u043f\u043b\u043e\u0442\u043d\u044b\u0435, R/R \u043c\u043e\u0436\u043d\u043e 1:1.5, \u0434\u043e\u043b\u044f \u0434\u0435\u043f\u043e \u043c\u0435\u043b\u043a\u0430\u044f.",
)

SWING = HorizonPack(
    key="swing",
    label="7-14 \u0434\u043d\u0435\u0439",
    candle_tf="1d",
    candles_back=30,
    news_window_h=72,
    stop_atr_mult=2.5,
    stop_pct_crypto=0.05,
    stop_pct_stocks=0.03,
    min_rr=2.0,
    size_caps={"risk_on": 0.15, "neutral": 0.08, "risk_off": 0.03},
    description="\u0414\u0435\u0444\u043e\u043b\u0442. \u0421\u0432\u0438\u043d\u0433 \u043f\u043e\u0434 \u0441\u0440\u0435\u0434\u043d\u0435\u0441\u0440\u043e\u0447\u043d\u044b\u0439 \u0441\u0446\u0435\u043d\u0430\u0440\u0438\u0439 \u0441 \u0443\u0432\u0430\u0436\u0435\u043d\u0438\u0435\u043c \u043a ATR \u0438 \u043a\u043b\u044e\u0447\u0435\u0432\u044b\u043c \u0443\u0440\u043e\u0432\u043d\u044f\u043c.",
)

POSITION = HorizonPack(
    key="position",
    label="30+ \u0434\u043d\u0435\u0439",
    candle_tf="1w",
    candles_back=26,
    news_window_h=168,
    stop_atr_mult=3.5,
    stop_pct_crypto=0.10,
    stop_pct_stocks=0.05,
    min_rr=3.0,
    size_caps={"risk_on": 0.20, "neutral": 0.12, "risk_off": 0.05},
    description="\u041a\u0440\u0443\u043f\u043d\u044b\u0439 \u0442\u0440\u0435\u043d\u0434 / \u043c\u0430\u043a\u0440\u043e-\u043f\u043e\u0437\u0438\u0446\u0438\u044f. \u0421\u0442\u043e\u043f \u0448\u0438\u0440\u0435, R/R \u2265 1:3, \u0434\u043e\u043b\u044f \u0431\u043e\u043b\u044c\u0448\u0435 \u043d\u043e \u043e\u0441\u0442\u043e\u0440\u043e\u0436\u043d\u0435\u0435 \u043f\u043e \u0432\u0445\u043e\u0434\u0443.",
)


HORIZONS: dict[str, HorizonPack] = {
    "intraday": INTRADAY,
    "swing": SWING,
    "position": POSITION,
}

DEFAULT_HORIZON_KEY = "swing"


def get_horizon(key: str | None) -> HorizonPack:
    """Resolve a (possibly empty) string to a horizon pack with a safe default."""
    if not key:
        return HORIZONS[DEFAULT_HORIZON_KEY]
    norm = str(key).strip().lower()
    if norm in HORIZONS:
        return HORIZONS[norm]
    aliases = {
        "intra": "intraday",
        "intraday": "intraday",
        "\u0438\u043d\u0442\u0440\u0430\u0434\u0435\u0439": "intraday",
        "intra-day": "intraday",
        "1-3": "intraday",
        "1-3\u0434": "intraday",
        "swing": "swing",
        "\u0441\u0432\u0438\u043d\u0433": "swing",
        "7-14": "swing",
        "7-14\u0434": "swing",
        "\u0441\u0442\u0430\u043d\u0434\u0430\u0440\u0442": "swing",
        "standard": "swing",
        "default": "swing",
        "position": "position",
        "\u043f\u043e\u0437\u0438\u0446\u0438\u044f": "position",
        "\u043f\u043e\u0437\u0438\u0446\u0438\u043e\u043d\u043d\u044b\u0439": "position",
        "30+": "position",
        "30+\u0434": "position",
        "long-term": "position",
    }
    return HORIZONS.get(aliases.get(norm, DEFAULT_HORIZON_KEY), HORIZONS[DEFAULT_HORIZON_KEY])


def synth_overlay(pack: HorizonPack) -> str:
    """Horizon-specific overlay appended to SYNTH_SYSTEM.

    Dynamic calibration (pre-live-hardening, Requirement D):
    Reads live BULL/BEAR/NEUTRAL hit-rates from AUTO_TRACK.md (30-day window).
    Falls back to hardcoded April 2026 snapshot if data unavailable.
    """
    from core.calibration_cache import get_calibration

    caps = pack.size_caps
    risk_on = int(caps.get("risk_on", 0.10) * 100)
    neutral = int(caps.get("neutral", 0.05) * 100)
    risk_off = int(caps.get("risk_off", 0.02) * 100)
    pct_crypto = int(pack.stop_pct_crypto * 100)
    pct_stocks = int(pack.stop_pct_stocks * 100)

    cal = get_calibration()
    snapshot_note = " (cached snapshot)" if cal.is_snapshot else ""
    insufficient_note = ""
    if cal.is_snapshot and cal.obs < 10:
        insufficient_note = " (\u043d\u0435\u0434\u043e\u0441\u0442\u0430\u0442\u043e\u0447\u043d\u043e \u0434\u0430\u043d\u043d\u044b\u0445, \u043f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u044e \u0438\u0441\u0442\u043e\u0440\u0438\u0447\u0435\u0441\u043a\u0438\u0439 \u0441\u043d\u044d\u043f\u0448\u043e\u0442)"

    return (
        f"\n\n\u2550 \u0413\u041e\u0420\u0418\u0417\u041e\u041d\u0422 {pack.label} \u2550\n"
        f"\u0421\u0442\u043e\u043f: {pack.stop_atr_mult:g}\u00d7ATR (\u0438\u043b\u0438 {pct_crypto}%/{pct_stocks}% \u043a\u0440\u0438\u043f\u0442\u0430/\u0430\u043a\u0446\u0438\u0438).\n"
        f"R/R \u043c\u0438\u043d 1:{pack.min_rr:g}. \u0420\u0430\u0437\u043c\u0435\u0440: {risk_on}/{neutral}/{risk_off}% \u043f\u0440\u0438 risk-on/neutral/risk-off.\n"
        f"\u0422\u0440\u0438\u0433\u0433\u0435\u0440\u044b: {pack.candle_tf}-\u0441\u0432\u0435\u0447\u0438, \u043d\u043e\u0432\u043e\u0441\u0442\u0438 \u2264 {pack.news_window_h}\u0447.\n"
        f"\u0412 \u043a\u0430\u0436\u0434\u043e\u043c plan: \"horizon\":\"{pack.label}\". \u041f\u043b\u0430\u043d \u0441 R/R \u043d\u0438\u0436\u0435 \u043c\u0438\u043d\u0438\u043c\u0443\u043c\u0430 \u2192 CASH.\n"
        "\u041d\u0430\u0442\u044f\u0436\u0435\u043a \u043d\u0435\u0442: \u0434\u0430\u043d\u043d\u044b\u0445 \u043c\u0430\u043b\u043e / \u0441\u0435\u0442\u0430\u043f \u0448\u0430\u0442\u043a\u0438\u0439 \u2014 CASH.\n"
        f"\u2550 \u041a\u0410\u041b\u0418\u0411\u0420\u041e\u0412\u041a\u0410 ({cal.window}, {cal.obs} \u043d\u0430\u0431\u043b\u044e\u0434\u0435\u043d\u0438\u0439){snapshot_note}{insufficient_note} \u2550\n"
        f"BULL \u0432\u0435\u0440\u0434\u0438\u043a\u0442\u044b \u0441\u0440\u0430\u0431\u043e\u0442\u0430\u043b\u0438 \u0432 {cal.bull_pct}% \u0441\u043b\u0443\u0447\u0430\u0435\u0432, BEAR \u0432 {cal.bear_pct}% (\u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u0447\u0435\u0441\u043a\u0438 \u0445\u0443\u0436\u0435 \u043c\u043e\u043d\u0435\u0442\u043a\u0438),\n"
        f"NEUTRAL \u0432 {cal.neutral_pct}%. \u041f\u0435\u0440\u0435\u0434 \u0432\u044b\u0434\u0430\u0447\u0435\u0439 BEAR \u2014 \u0441\u043f\u0440\u043e\u0441\u0438 \u00ab\u0435\u0441\u0442\u044c \u043b\u0438 2+ \u043a\u043e\u043d\u043a\u0440\u0435\u0442\u043d\u044b\u0445 \u0440\u0438\u0441\u043a\u0430 \u0441 \u0446\u0438\u0444\u0440\u0430\u043c\u0438\n"
        "\u0418\u0417 \u041a\u041e\u041d\u0422\u0415\u041a\u0421\u0422\u0410, \u043d\u0435 \u201e\u0432\u043e\u0437\u043c\u043e\u0436\u043d\u044b\u0435\u201c?\u00bb. \u0415\u0441\u043b\u0438 \u043d\u0435\u0442 \u2014 verdict = \u041d\u0415\u0419\u0422\u0420\u0410\u041b\u042c\u041d\u042b\u0419, plan = CASH.\n"
        "\u0426\u0435\u043d\u044b entry/stop/target \u2014 \u0422\u041e\u041b\u042c\u041a\u041e \u0438\u0437 \u0431\u043b\u043e\u043a\u0430 \u00ab\u0420\u0415\u0410\u041b\u042c\u041d\u042b\u0415 \u0420\u042b\u041d\u041e\u0427\u041d\u042b\u0415 \u0414\u0410\u041d\u041d\u042b\u0415\u00bb \u043d\u0430\u0432\u0435\u0440\u0445\u0443\n"
        "(\u0435\u0441\u043b\u0438 entry \u0443\u0435\u0445\u0430\u043b >3% \u043e\u0442 \u0442\u0435\u043a\u0443\u0449\u0435\u0439 \u0446\u0435\u043d\u044b \u2014 \u0441\u0438\u0441\u0442\u0435\u043c\u0430 \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438 \u043f\u0435\u0440\u0435\u0432\u0435\u0434\u0451\u0442 \u0432 CASH).\n"
    )


def speechwriter_horizon_line(pack: HorizonPack) -> str:
    """Single line for Speechwriter / digest header."""
    return f"\u23f1 \u0413\u043e\u0440\u0438\u0437\u043e\u043d\u0442: {pack.label_pretty}"


def all_horizon_keys() -> list[str]:
    return list(HORIZONS.keys())
