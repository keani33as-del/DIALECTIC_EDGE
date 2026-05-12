"""
core/calibration_cache.py — Динамическая калибровка BULL/BEAR/NEUTRAL hit-rate.

Pre-live-hardening, Requirement D:
Вместо захардкоженных «BULL 50%, BEAR 14%, NEUTRAL 33%» (апрель 2026) —
читаем реальные цифры из AUTO_TRACK.md за последние 30 дней.

Fallback: если AUTO_TRACK.md недоступен или данных мало (<10 closed VERDICT) —
используем исторический snapshot.

Env-переменные:
  AUTO_TRACK_PATH — путь к AUTO_TRACK.md (default: AUTO_TRACK.md в корне)
  KIRO_DISABLE_DYNAMIC_CALIBRATION=1 — всегда snapshot (для отладки)
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 3600  # 1 час
_MIN_OBSERVATIONS = 10     # D3: ниже — fallback к snapshot
_WINDOW_DAYS = 30
_AUTO_TRACK_PATH = Path(os.getenv("AUTO_TRACK_PATH", "AUTO_TRACK.md"))

# Захардкоженный snapshot — fallback (D2/D3)
_SNAPSHOT = {
    "bull_pct": 50,
    "bear_pct": 14,
    "neutral_pct": 33,
    "obs": 25,
    "window": "апрель 2026",
    "is_snapshot": True,
}


@dataclass
class Calibration:
    bull_pct: int
    bear_pct: int
    neutral_pct: int
    obs: int
    window: str
    is_snapshot: bool


# In-memory cache
_cache: dict = {"at": 0.0, "value": None}


def get_calibration() -> Calibration:
    """Получить калибровку. Кэш с TTL 1ч, fallback к snapshot."""
    if os.getenv("KIRO_DISABLE_DYNAMIC_CALIBRATION", "").strip() == "1":
        return Calibration(**_SNAPSHOT)

    now = time.time()
    if _cache["value"] and (now - _cache["at"]) < _CACHE_TTL_SECONDS:
        return _cache["value"]

    parsed = _parse_auto_track()
    if parsed and parsed.obs >= _MIN_OBSERVATIONS:
        _cache["value"] = parsed
        _cache["at"] = now
        return parsed

    fallback = Calibration(**_SNAPSHOT)
    _cache["value"] = fallback
    _cache["at"] = now
    return fallback


def _parse_auto_track() -> Optional[Calibration]:
    """Парсит AUTO_TRACK.md, считает hit-rate по closed VERDICT-прогнозам за 30 дней."""
    if not _AUTO_TRACK_PATH.exists():
        logger.debug("[CALIBRATION] AUTO_TRACK.md не найден, fallback к snapshot")
        return None
    try:
        text = _AUTO_TRACK_PATH.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"[CALIBRATION] Ошибка чтения AUTO_TRACK.md: {e}")
        return None

    cutoff = datetime.utcnow() - timedelta(days=_WINDOW_DAYS)

    # Ищем строки таблицы с VERDICT (closed: ✅ или ❌)
    # Формат AUTO_TRACK.md (из AutoTracker.generate_markdown):
    # | 11.05.2026 | Daily Digest | VERDICT | BULLISH | ✅ Верно | 100% | +2.1% |
    row_re = re.compile(
        r"\|\s*(\d{2}\.\d{2}\.\d{4})\s*\|[^|]*\|\s*VERDICT\s*\|\s*"
        r"(BULLISH|BEARISH|NEUTRAL)\s*\|\s*([^|]+)\|",
        re.IGNORECASE
    )

    buckets = {"BULLISH": [0, 0], "BEARISH": [0, 0], "NEUTRAL": [0, 0]}  # [wins, total]

    for m in row_re.finditer(text):
        date_str, verdict, result = m.group(1), m.group(2).upper(), m.group(3)
        try:
            d = datetime.strptime(date_str, "%d.%m.%Y")
        except ValueError:
            continue
        if d < cutoff:
            continue
        if "✅" in result:
            buckets[verdict][0] += 1
            buckets[verdict][1] += 1
        elif "❌" in result:
            buckets[verdict][1] += 1
        # ⚠️ pending — пропускаем (не closed)

    total_obs = sum(b[1] for b in buckets.values())
    if total_obs < _MIN_OBSERVATIONS:
        logger.debug(f"[CALIBRATION] Мало данных: {total_obs} < {_MIN_OBSERVATIONS}, fallback")
        return None

    def pct(v: str) -> int:
        wins, total = buckets[v]
        return int(round(wins / total * 100)) if total > 0 else 0

    return Calibration(
        bull_pct=pct("BULLISH"),
        bear_pct=pct("BEARISH"),
        neutral_pct=pct("NEUTRAL"),
        obs=total_obs,
        window=f"{_WINDOW_DAYS}д (live)",
        is_snapshot=False,
    )
