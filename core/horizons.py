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
        emoji = {"intraday": "⚡", "swing": "📈", "position": "🏔"}.get(self.key, "🎯")
        return f"{emoji} {self.label}"


INTRADAY = HorizonPack(
    key="intraday",
    label="1-3 дня",
    candle_tf="4h",
    candles_back=30,
    news_window_h=24,
    stop_atr_mult=1.5,
    stop_pct_crypto=0.02,
    stop_pct_stocks=0.01,
    min_rr=1.5,
    size_caps={"risk_on": 0.05, "neutral": 0.03, "risk_off": 0.01},
    description="Скальп / краткосрочные триггеры. Стопы плотные, R/R можно 1:1.5, доля депо мелкая.",
)

SWING = HorizonPack(
    key="swing",
    label="7-14 дней",
    candle_tf="1d",
    candles_back=30,
    news_window_h=72,
    stop_atr_mult=2.5,
    stop_pct_crypto=0.05,
    stop_pct_stocks=0.03,
    min_rr=2.0,
    size_caps={"risk_on": 0.15, "neutral": 0.08, "risk_off": 0.03},
    description="Дефолт. Свинг под среднесрочный сценарий с уважением к ATR и ключевым уровням.",
)

POSITION = HorizonPack(
    key="position",
    label="30+ дней",
    candle_tf="1w",
    candles_back=26,
    news_window_h=168,
    stop_atr_mult=3.5,
    stop_pct_crypto=0.10,
    stop_pct_stocks=0.05,
    min_rr=3.0,
    size_caps={"risk_on": 0.20, "neutral": 0.12, "risk_off": 0.05},
    description="Крупный тренд / макро-позиция. Стоп шире, R/R ≥ 1:3, доля больше но осторожнее по входу.",
)


HORIZONS: dict[str, HorizonPack] = {
    "intraday": INTRADAY,
    "swing": SWING,
    "position": POSITION,
}

DEFAULT_HORIZON_KEY = "swing"


def get_horizon(key: str | None) -> HorizonPack:
    """Resolve a (possibly empty) string to a horizon pack with a safe default.

    Accepts: keys ("intraday"/"swing"/"position"), Russian aliases ("интрадей"
    /"свинг"/"позиция"/"позиционный"), or anything else falsy → default swing.
    """
    if not key:
        return HORIZONS[DEFAULT_HORIZON_KEY]
    norm = str(key).strip().lower()
    if norm in HORIZONS:
        return HORIZONS[norm]
    aliases = {
        "intra": "intraday",
        "intraday": "intraday",
        "интрадей": "intraday",
        "intra-day": "intraday",
        "1-3": "intraday",
        "1-3д": "intraday",
        "swing": "swing",
        "свинг": "swing",
        "7-14": "swing",
        "7-14д": "swing",
        "стандарт": "swing",
        "standard": "swing",
        "default": "swing",
        "position": "position",
        "позиция": "position",
        "позиционный": "position",
        "30+": "position",
        "30+д": "position",
        "long-term": "position",
    }
    return HORIZONS.get(aliases.get(norm, DEFAULT_HORIZON_KEY), HORIZONS[DEFAULT_HORIZON_KEY])


def synth_overlay(pack: HorizonPack) -> str:
    """Horizon-specific overlay appended to SYNTH_SYSTEM.

    Сжатый формат — мы упёрлись в лимит контекста Cerebras (8192 токенов).
    Старый «дружественный» оверлей (~600 chars) добавлял ~250 токенов и
    отправлял Bull/Bear/Synth в Mistral fallback. Сжали до ~200 chars
    без потери критичных правил.
    """
    caps = pack.size_caps
    risk_on = int(caps.get("risk_on", 0.10) * 100)
    neutral = int(caps.get("neutral", 0.05) * 100)
    risk_off = int(caps.get("risk_off", 0.02) * 100)
    pct_crypto = int(pack.stop_pct_crypto * 100)
    pct_stocks = int(pack.stop_pct_stocks * 100)

    return (
        f"\n\n═ ГОРИЗОНТ {pack.label} ═\n"
        f"Стоп: {pack.stop_atr_mult:g}×ATR (или {pct_crypto}%/{pct_stocks}% крипта/акции).\n"
        f"R/R мин 1:{pack.min_rr:g}. Размер: {risk_on}/{neutral}/{risk_off}% при risk-on/neutral/risk-off.\n"
        f"Триггеры: {pack.candle_tf}-свечи, новости ≤ {pack.news_window_h}ч.\n"
        f"В каждом plan: \"horizon\":\"{pack.label}\". План с R/R ниже минимума → CASH.\n"
        "Натяжек нет: данных мало / сетап шаткий — CASH.\n"
    )


def speechwriter_horizon_line(pack: HorizonPack) -> str:
    """Single line for Speechwriter / digest header."""
    return f"⏱ Горизонт: {pack.label_pretty}"


def all_horizon_keys() -> list[str]:
    return list(HORIZONS.keys())
