"""
regime_detector.py — Определение рыночного режима.

Классифицирует рынок на:
  UPTREND   — агрессивные лонги, широкие стопы
  DOWNTREND — шорты или кэш, узкие стопы
  SIDEWAYS  — скальпинг или отдых, очень узкие стопы
  HIGH_VOL  — уменьшенные позиции, широкие стопы

Использует:
  - MA50/MA200 crossover
  - ADX (сила тренда)
  - ATR (волатильность)
  - RSI (перекупленность/перепроданность)
  - Volume trend
  - Hurst exponent + Shannon entropy (опциональный фильтр торгуемости,
    см. core/market_complexity.py — режет confidence в random-walk режиме)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

try:
    from .market_complexity import analyze_complexity, MarketComplexity
except ImportError:  # pragma: no cover — на случай если модуль не нашёлся
    analyze_complexity = None  # type: ignore[assignment]
    MarketComplexity = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


@dataclass
class MarketRegime:
    """Описание текущего режима рынка."""
    regime: str           # UPTREND, DOWNTREND, SIDEWAYS, HIGH_VOL
    confidence: float     # 0.0-1.0
    trend_strength: float # ADX-like metric, 0-100
    volatility_pct: float # ATR as % of price
    rsi: float            # 0-100
    ma_signal: str        # BULLISH_CROSS, BEARISH_CROSS, ABOVE_MA, BELOW_MA
    volume_trend: str     # INCREASING, DECREASING, NEUTRAL
    recommendation: str   # Actionable advice
    # ── Дополнительные метрики «торгуемости» (опционально) ────────────────
    # Заполняются если len(candles) >= ~65 и market_complexity модуль доступен.
    # Позволяют отфильтровать random-walk режим до открытия позиции.
    hurst: Optional[float] = None              # 0..1, ~0.5 = случайное блуждание
    entropy_normalized: Optional[float] = None  # 0..1, чем выше — тем хаотичнее
    tradeable_score: Optional[float] = None     # 0..1, < 0.3 = не торговать
    complexity_hint: Optional[str] = None       # TRENDING/RANDOM_WALK/CHAOTIC/...

    def to_dict(self) -> dict:
        out = {
            "regime": self.regime,
            "confidence": round(self.confidence, 2),
            "trend_strength": round(self.trend_strength, 1),
            "volatility_pct": round(self.volatility_pct, 2),
            "rsi": round(self.rsi, 1),
            "ma_signal": self.ma_signal,
            "volume_trend": self.volume_trend,
            "recommendation": self.recommendation,
        }
        # Добавляем complexity-метрики только если они посчитались
        if self.hurst is not None:
            out["hurst"] = round(self.hurst, 3)
        if self.entropy_normalized is not None:
            out["entropy_normalized"] = round(self.entropy_normalized, 3)
        if self.tradeable_score is not None:
            out["tradeable_score"] = round(self.tradeable_score, 3)
        if self.complexity_hint is not None:
            out["complexity_hint"] = self.complexity_hint
        return out


class RegimeDetector:
    """
    Определяет рыночный режим на основе OHLCV данных.
    
    Логика:
    1. MA50 vs MA200 → долгосрочный тренд
    2. Цена vs MA20 → среднесрочный тренд
    3. ADX → сила тренда
    4. ATR → волатильность
    5. RSI → перекупленность/перепроданность
    6. Volume → подтверждение
    """

    def __init__(
        self,
        ma_fast: int = 50,
        ma_slow: int = 200,
        rsi_period: int = 14,
        atr_period: int = 14,
        adx_period: int = 14,
        high_vol_threshold: float = 5.0,  # ATR % > 5% = HIGH_VOL
        low_adx_threshold: float = 20.0,   # ADX < 20 = SIDEWAYS
    ):
        self.ma_fast = ma_fast
        self.ma_slow = ma_slow
        self.rsi_period = rsi_period
        self.atr_period = atr_period
        self.adx_period = adx_period
        self.high_vol_threshold = high_vol_threshold
        self.low_adx_threshold = low_adx_threshold

    def detect(self, candles: list) -> Optional[MarketRegime]:
        """
        Определить режим рынка по свечам.
        candles: list of dict или object с open/high/low/close/volume
        """
        if not candles or len(candles) < self.ma_slow:
            return None

        closes = self._extract(candles, "close")
        highs = self._extract(candles, "high")
        lows = self._extract(candles, "low")
        volumes = self._extract(candles, "volume")

        # 1. Скользящие средние
        ma_fast = self._sma(closes, self.ma_fast)
        ma_slow = self._sma(closes, self.ma_slow)
        ma_20 = self._sma(closes, 20)

        if not ma_fast or not ma_slow or not ma_20:
            return None

        current_price = closes[-1]
        
        # MA сигнал
        if ma_fast > ma_slow:
            ma_signal = "BULLISH_CROSS"
        elif ma_fast < ma_slow:
            ma_signal = "BEARISH_CROSS"
        elif current_price > ma_20:
            ma_signal = "ABOVE_MA"
        else:
            ma_signal = "BELOW_MA"

        # 2. RSI
        rsi = self._rsi(closes, self.rsi_period)

        # 3. ATR как % цены
        atr = self._atr(highs, lows, closes, self.atr_period)
        atr_pct = (atr / current_price * 100) if current_price > 0 else 0

        # 4. ADX (упрощенный)
        adx = self._adx(highs, lows, closes, self.adx_period)

        # 5. Volume trend
        volume_trend = self._volume_trend(volumes)

        # === КЛАССИФИКАЦИЯ РЕЖИМА ===
        regime = "SIDEWAYS"
        confidence = 0.5
        recommendation = "Нейтральный режим. Уменьшенные позиции или отдых."

        # HIGH_VOL приоритетнее всего
        if atr_pct > self.high_vol_threshold:
            regime = "HIGH_VOL"
            confidence = min(0.9, atr_pct / 10)
            recommendation = (
                f"Высокая волатильность ({atr_pct:.1f}%). "
                "Уменьшить размер позиции на 50%. Широкие стопы."
            )
        # UPTREND
        elif ma_signal == "BULLISH_CROSS" and adx > self.low_adx_threshold and current_price > ma_20:
            regime = "UPTREND"
            confidence = min(0.95, (adx / 100) * 0.6 + (0.4 if ma_fast > ma_slow * 1.05 else 0.2))
            if rsi > 70:
                recommendation = (
                    f"Бычий тренд, но RSI={rsi:.0f} (перекупленность). "
                    "Ждать отката для входа."
                )
            else:
                recommendation = (
                    f"Бычий тренд подтверждён (ADX={adx:.0f}). "
                    "Агрессивные лонги, стандартные стопы."
                )
        # DOWNTREND
        elif ma_signal == "BEARISH_CROSS" and adx > self.low_adx_threshold and current_price < ma_20:
            regime = "DOWNTREND"
            confidence = min(0.95, (adx / 100) * 0.6 + (0.4 if ma_fast < ma_slow * 0.95 else 0.2))
            if rsi < 30:
                recommendation = (
                    f"Медвежий тренд, но RSI={rsi:.0f} (перепроданность). "
                    "Возможен отскок. Осторожно с шортами."
                )
            else:
                recommendation = (
                    f"Медвежий тренд подтверждён (ADX={adx:.0f}). "
                    "Шорты или кэш. Узкие стопы."
                )
        # SIDEWAYS
        else:
            regime = "SIDEWAYS"
            confidence = max(0.5, 1.0 - (adx / 100))
            recommendation = (
                f"Боковик (ADX={adx:.0f}). "
                "Скальпинг от границ диапазона или отдых."
            )

        # === ОПЦИОНАЛЬНО: Hurst + Shannon entropy фильтр ===
        # На длинных историях (>=65 баров) считаем «торгуемость» рынка как
        # независимый математический фильтр. В random-walk / хаотичном режиме
        # любая стратегия даёт ~50/50 — режем confidence чтобы UPTREND/DOWNTREND
        # не открывали позиции на шуме.
        hurst_val: Optional[float] = None
        entropy_val: Optional[float] = None
        tradeable_val: Optional[float] = None
        complexity_hint: Optional[str] = None

        if analyze_complexity is not None:
            try:
                complexity = analyze_complexity(closes)
            except Exception as e:
                logger.warning("market_complexity failed: %s", e)
                complexity = None

            if complexity is not None:
                hurst_val = complexity.hurst
                entropy_val = complexity.entropy_normalized
                tradeable_val = complexity.tradeable_score
                complexity_hint = complexity.regime_hint

                # Мягкая корректировка: если рынок «не торгуем» по математике,
                # то даже UPTREND/DOWNTREND не должен брать позицию на полную.
                # Не меняем сам regime — только confidence + recommendation.
                if tradeable_val < 0.3 and regime in ("UPTREND", "DOWNTREND"):
                    confidence = min(confidence, 0.4)
                    recommendation = (
                        f"{recommendation} ⚠️ Hurst={hurst_val:.2f}, "
                        f"энтропия={entropy_val:.2f} — рынок в режиме "
                        f"{complexity_hint}, сигналы тренда ненадёжны. "
                        "Уменьшить размер позиции или подождать."
                    )
                elif tradeable_val > 0.7 and regime in ("UPTREND", "DOWNTREND"):
                    # Лёгкий буст confidence в чисто трендовом режиме
                    confidence = min(0.98, confidence * 1.05)

        return MarketRegime(
            regime=regime,
            confidence=confidence,
            trend_strength=adx,
            volatility_pct=atr_pct,
            rsi=rsi,
            ma_signal=ma_signal,
            volume_trend=volume_trend,
            recommendation=recommendation,
            hurst=hurst_val,
            entropy_normalized=entropy_val,
            tradeable_score=tradeable_val,
            complexity_hint=complexity_hint,
        )

    def get_position_size_multiplier(self, regime: MarketRegime) -> float:
        """Множитель размера позиции в зависимости от режима."""
        multipliers = {
            "UPTREND": 1.0,
            "DOWNTREND": 0.7,
            "SIDEWAYS": 0.4,
            "HIGH_VOL": 0.5,
        }
        base = multipliers.get(regime.regime, 0.5)
        # Корректировка по уверенности
        return base * (0.5 + regime.confidence * 0.5)

    def get_stop_multiplier(self, regime: MarketRegime) -> float:
        """Множитель для стоп-лосса (шире в волатильном рынке)."""
        multipliers = {
            "UPTREND": 1.0,
            "DOWNTREND": 0.8,
            "SIDEWAYS": 0.6,
            "HIGH_VOL": 1.5,
        }
        return multipliers.get(regime.regime, 1.0)

    # ─── Вспомогательные методы ──────────────────────────────────────────────

    @staticmethod
    def _extract(data, key: str) -> list[float]:
        if hasattr(data[0], key):
            return [float(getattr(c, key)) for c in data]
        if isinstance(data[0], dict):
            return [float(c.get(key, 0)) for c in data]
        return []

    @staticmethod
    def _sma(values: list[float], period: int) -> Optional[float]:
        if len(values) < period:
            return None
        return sum(values[-period:]) / period

    @staticmethod
    def _rsi(closes: list[float], period: int) -> float:
        if len(closes) < period + 1:
            return 50.0

        gains = []
        losses = []
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i - 1]
            gains.append(max(0, delta))
            losses.append(max(0, -delta))

        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> float:
        if len(highs) < period + 1:
            return 0.0

        true_ranges = []
        for i in range(1, len(highs)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            true_ranges.append(tr)

        return sum(true_ranges[-period:]) / period

    @staticmethod
    def _adx(highs: list[float], lows: list[float], closes: list[float], period: int) -> float:
        """Упрощенный ADX."""
        if len(highs) < period + 1:
            return 20.0

        plus_dm = []
        minus_dm = []
        for i in range(1, len(highs)):
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]

            if up_move > down_move and up_move > 0:
                plus_dm.append(up_move)
            else:
                plus_dm.append(0)

            if down_move > up_move and down_move > 0:
                minus_dm.append(down_move)
            else:
                minus_dm.append(0)

        atr = RegimeDetector._atr(highs, lows, closes, period)
        if atr == 0:
            return 20.0

        plus_di = (sum(plus_dm[-period:]) / period) / atr * 100
        minus_di = (sum(minus_dm[-period:]) / period) / atr * 100

        if plus_di + minus_di == 0:
            return 20.0

        dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100
        return dx

    @staticmethod
    def _volume_trend(volumes: list[float]) -> str:
        if len(volumes) < 20:
            return "NEUTRAL"

        recent = sum(volumes[-5:]) / 5
        older = sum(volumes[-20:-5]) / 15

        if recent > older * 1.2:
            return "INCREASING"
        elif recent < older * 0.8:
            return "DECREASING"
        return "NEUTRAL"
