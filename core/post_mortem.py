"""core/post_mortem.py — анализ "что сказали vs что произошло" через 24ч.

Зачем: каждый /daily выдаёт вердикт (BULLISH / BEARISH / NEUTRAL) + per-asset
направления (BTC BULLISH, ETH BEARISH …). Через сутки реальный рынок отвечает.
Если мы это никогда не сверяем — у нас нет honest feedback loop, а калибровка
(PR #24/#25) живёт только на /signal и /markets, не на /daily.

Post-mortem закрывает эту дыру:

  1. Парсит DIGEST_CACHE.md, достаёт вердикт+per-asset вчерашнего дайджеста
     (через `auto_tracker.DigestParser`, тот же парсер что уже валидируется
     в AUTO_TRACK.md → не дублируем regex).
  2. Через `auto_tracker.PriceFetcher` достаёт цену актива на момент дайджеста
     (entry) и сейчас (eval) → return_pct.
  3. `classify_outcome(direction, return_pct)` → "hit" / "miss" / "flat" /
     "neutral_correct" / "neutral_missed".
  4. Пишет результат в `predictions` (`prediction_type='daily_digest'`,
     `report_type='post_mortem'`) — это делает label видимым для калибровки
     через её существующий fuzzy-join по asset+direction+time.
  5. Линкует с `decision_provenance` (через `link_prediction`) когда находит
     совпадение — тогда score breakdown появляется в explanation.
  6. Markdown + Telegram форматтеры — для /postmortem и POSTMORTEM.md.

Feature flag: `FEATURE_POST_MORTEM` (config.py). По дефолту 1 — модуль импорт-
безопасный (вся БД-инициализация ленивая, нет тяжёлых импортов на module-load).

Без сети: все функции принимают dependency-injected fetcher через аргументы,
чтобы можно было тестировать без yfinance/aiohttp.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

import aiosqlite

from config import DB_PATH

logger = logging.getLogger(__name__)

# ─── КОНСТАНТЫ ───────────────────────────────────────────────────────────────

# % движения, ниже которого считаем "рынок не подтвердил".  По крипте за сутки
# 0.5% — это ~0.3 σ̂, уровень шума.  Можно тюнить через POST_MORTEM_FLAT_PCT.
FLAT_THRESHOLD_PCT = float(os.getenv("POST_MORTEM_FLAT_PCT", "0.5"))

# Горизонт оценки.  Daily-дайджест по дефолту смотрит на 7-14д, но post-mortem
# вернёт результат за первые 24ч.  Это не противоречие: 24ч — это honesty-check
# "ушёл ли рынок в ту сторону, что мы предсказали".  Полный 7д — отдельная
# история (см. follow-up в /postmortem 7).
DEFAULT_HORIZON_HOURS = int(os.getenv("POST_MORTEM_HORIZON_HOURS", "24"))

# Yahoo-тикеры для активов, которые мы реально умеем оценивать.  None — для
# индикаторов типа Fear&Greed, которые не имеют цены и пропускаются.
ASSET_TO_YAHOO: dict[str, Optional[str]] = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "BNB": "BNB-USD",
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
    "S&P": "^GSPC",
    "VIX": "^VIX",
    "Gold": "GC=F",
    "Нефть": "CL=F",
    "Fear&Greed": None,
}

# Когда оцениваем "вердикт" дайджеста (VERDICT BULLISH / BEARISH / NEUTRAL) —
# смотрим на этот актив как прокси (вердикт глобальный, но рынок без anchor'а
# не оценить).  BTC — самая ликвидная единица "крипто-настроения".
VERDICT_PROXY_ASSET = "BTC"

# Синонимы направления.  Daily-дайджест использует BULLISH/BEARISH/NEUTRAL,
# /markets — LONG/SHORT.  Нормализуем в три класса.
_BULL_TOKENS = ("BULLISH", "BULL", "LONG", "BUY", "БЫЧ", "🐂")
_BEAR_TOKENS = ("BEARISH", "BEAR", "SHORT", "SELL", "МЕДВ", "🐻")
_NEUT_TOKENS = ("NEUTRAL", "НЕЙТРАЛ", "🟡", "WAIT")


# ─── ТИПЫ ────────────────────────────────────────────────────────────────────


@dataclass
class PostMortemEntry:
    """Один проверенный пункт дайджеста (вердикт или per-asset направление)."""

    asset: str
    direction: str  # "BULLISH" / "BEARISH" / "NEUTRAL"
    forecast_date: str  # "18.05.2026" или "18.05.2026 08:13"
    entry_price: Optional[float]
    eval_price: Optional[float]
    return_pct: Optional[float]
    outcome: str  # "hit" / "miss" / "flat" / "neutral_correct" / "neutral_missed" / "no_data"
    explanation: str
    prediction_id: Optional[int] = None
    provenance_id: Optional[int] = None
    score_breakdown: Optional[dict[str, float]] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PostMortemReport:
    """Сводка по всем пунктам одного дайджеста."""

    digest_date: str
    horizon_hours: int
    entries: list[PostMortemEntry] = field(default_factory=list)

    @property
    def stats(self) -> dict[str, Any]:
        """Counts + hit-rate.  Считаем только пункты с known outcome."""
        return compute_stats(self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest_date": self.digest_date,
            "horizon_hours": self.horizon_hours,
            "entries": [e.to_dict() for e in self.entries],
            "stats": self.stats,
        }


# ─── НОРМАЛИЗАЦИЯ ────────────────────────────────────────────────────────────


def normalize_direction(direction: str) -> str:
    """Приводит direction к BULLISH / BEARISH / NEUTRAL."""
    if not direction:
        return "NEUTRAL"
    upper = direction.upper().strip()
    if any(tok in upper for tok in _BULL_TOKENS):
        return "BULLISH"
    if any(tok in upper for tok in _BEAR_TOKENS):
        return "BEARISH"
    if any(tok in upper for tok in _NEUT_TOKENS):
        return "NEUTRAL"
    return "NEUTRAL"


def direction_to_canonical_long_short(direction: str) -> str:
    """BULLISH→LONG, BEARISH→SHORT, NEUTRAL→NONE — для записи в predictions."""
    d = normalize_direction(direction)
    return {"BULLISH": "LONG", "BEARISH": "SHORT", "NEUTRAL": "NONE"}[d]


# ─── КЛАССИФИКАЦИЯ ИСХОДА ────────────────────────────────────────────────────


def classify_outcome(
    direction: str,
    return_pct: Optional[float],
    *,
    flat_threshold: float = FLAT_THRESHOLD_PCT,
) -> str:
    """Возвращает один из:

      • "hit"               — направление подтвердилось (movement > threshold).
      • "miss"              — рынок пошёл против.
      • "flat"              — направление было ненейтральным, но движения не
                              случилось (|return| ≤ threshold).
      • "neutral_correct"   — мы сказали NEUTRAL, рынок и правда не двинулся.
      • "neutral_missed"    — мы сказали NEUTRAL, а рынок ушёл.
      • "no_data"           — не смогли посчитать (нет цен).

    `return_pct` — изменение в процентах (положительное = вверх).
    """
    if return_pct is None:
        return "no_data"

    d = normalize_direction(direction)
    abs_move = abs(return_pct)

    if d == "NEUTRAL":
        return "neutral_correct" if abs_move <= flat_threshold else "neutral_missed"

    if abs_move <= flat_threshold:
        return "flat"

    if d == "BULLISH":
        return "hit" if return_pct > 0 else "miss"
    # BEARISH
    return "hit" if return_pct < 0 else "miss"


def outcome_to_prediction_result(outcome: str) -> str:
    """Маппинг в формат `predictions.result` (что читает calibration.py).

      • "hit", "neutral_correct"   → "win"
      • "miss", "neutral_missed"   → "loss"
      • "flat"                     → "caution"   (рынок не подтвердил, но и
                                                  не опроверг)
      • "no_data"                  → "expired"   (нечего сравнивать)
    """
    if outcome in ("hit", "neutral_correct"):
        return "win"
    if outcome in ("miss", "neutral_missed"):
        return "loss"
    if outcome == "flat":
        return "caution"
    return "expired"


# ─── ОБЪЯСНЕНИЕ ──────────────────────────────────────────────────────────────


_OUTCOME_RU = {
    "hit": "✅ Верно",
    "miss": "❌ Неверно",
    "flat": "⚪ Рынок не подтвердил (стоит)",
    "neutral_correct": "✅ Верно (рынок и правда стоит)",
    "neutral_missed": "❌ Сказали NEUTRAL, но рынок ушёл",
    "no_data": "⚠️ Нет данных по цене",
}


def explain_call(
    asset: str,
    direction: str,
    return_pct: Optional[float],
    outcome: str,
    score_breakdown: Optional[dict[str, float]] = None,
) -> str:
    """Короткая человеческая строка: что предсказали vs что произошло.

    Если есть `score_breakdown` (от linked provenance) — добавляем главный
    компонент, который "тащил" решение.
    """
    d = normalize_direction(direction)
    head = _OUTCOME_RU.get(outcome, outcome)
    move_str = "—" if return_pct is None else f"{return_pct:+.2f}%"

    pieces: list[str] = []
    pieces.append(f"{asset}: сказали {d.lower()}, рынок сделал {move_str} → {head}.")

    if score_breakdown:
        try:
            top_key, top_val = max(
                ((k, v) for k, v in score_breakdown.items() if isinstance(v, (int, float))),
                key=lambda kv: abs(kv[1]),
            )
            sign = "+" if top_val >= 0 else ""
            pieces.append(f"Главный драйвер решения: `{top_key}` ({sign}{top_val:.1f}).")
        except ValueError:
            pass

    if outcome == "miss":
        pieces.append("Калибровка получит negative-label — confidence по этому профилю снизится.")
    elif outcome == "hit":
        pieces.append("Калибровка получит positive-label — confidence по этому профилю вырастет.")

    return " ".join(pieces)


# ─── СТАТ ────────────────────────────────────────────────────────────────────


def compute_stats(entries: list[PostMortemEntry]) -> dict[str, Any]:
    """Считает counts + hit-rate.

    "Knowable" = всё кроме no_data.
    "Resolved" = hit/miss/neutral_correct/neutral_missed (без flat).
    `hit_rate` = (hit + neutral_correct) / resolved.
    """
    counts = {
        "hit": 0,
        "miss": 0,
        "flat": 0,
        "neutral_correct": 0,
        "neutral_missed": 0,
        "no_data": 0,
    }
    for e in entries:
        counts[e.outcome] = counts.get(e.outcome, 0) + 1

    knowable = sum(c for k, c in counts.items() if k != "no_data")
    resolved = counts["hit"] + counts["miss"] + counts["neutral_correct"] + counts["neutral_missed"]
    wins = counts["hit"] + counts["neutral_correct"]
    losses = counts["miss"] + counts["neutral_missed"]

    hit_rate = (wins / resolved) if resolved > 0 else None

    return {
        "total": len(entries),
        "knowable": knowable,
        "resolved": resolved,
        "wins": wins,
        "losses": losses,
        "flat": counts["flat"],
        "no_data": counts["no_data"],
        "hit_rate": hit_rate,
        "counts": counts,
    }


# ─── PARSING DIGEST ──────────────────────────────────────────────────────────


def _parse_digest_datetime(date_str: str) -> Optional[datetime]:
    """`'18.05.2026 08:13'` или `'18.05.2026'` → datetime.  None при ошибке."""
    if not date_str:
        return None
    raw = date_str.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def pick_target_digest(
    digests: list[dict],
    *,
    now: Optional[datetime] = None,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
    target_date: Optional[str] = None,
) -> Optional[dict]:
    """Из списка дайджестов выбирает тот, который надо разобрать сейчас.

    Если задан `target_date` — берём дайджест с этой датой (точное совпадение
    префикса до пробела).  Иначе — самый свежий дайджест, который старше
    `horizon_hours` (т.е. уже можно сравнивать с рынком).
    """
    if not digests:
        return None
    if now is None:
        now = datetime.now()

    if target_date:
        target_norm = target_date.strip().split()[0]
        for d in digests:
            if d.get("date", "").split()[0] == target_norm:
                return d
        return None

    candidates: list[tuple[datetime, dict]] = []
    threshold = now - timedelta(hours=horizon_hours)
    for d in digests:
        dt = _parse_digest_datetime(d.get("date", ""))
        if dt is None:
            continue
        if dt <= threshold:
            candidates.append((dt, d))
    if not candidates:
        return None
    candidates.sort(key=lambda kv: kv[0], reverse=True)
    return candidates[0][1]


# ─── PRICE LOOKUP ────────────────────────────────────────────────────────────


# Сигнатура fetcher'а: (yahoo_ticker, "DD.MM.YYYY [HH:MM]") → Optional[float].
# Историческая цена.  None если не удалось.
HistoricalPriceFn = Callable[[str, str], Awaitable[Optional[float]]]

# Сигнатура текущего fetcher'а: (yahoo_ticker,) → Optional[float].
CurrentPriceFn = Callable[[str], Awaitable[Optional[float]]]


async def _get_prices_for_asset(
    asset: str,
    forecast_date: str,
    historical_fn: HistoricalPriceFn,
    current_fn: CurrentPriceFn,
) -> tuple[Optional[float], Optional[float]]:
    """Возвращает (entry_price, eval_price) для актива.

    Маппит asset → Yahoo ticker.  Если активу нет цены (Fear&Greed) — (None, None).
    """
    ticker = ASSET_TO_YAHOO.get(asset)
    if ticker is None:
        return (None, None)

    entry = await historical_fn(ticker, forecast_date)
    current = await current_fn(ticker)
    return (entry, current)


# ─── ORCHESTRATION ───────────────────────────────────────────────────────────


async def evaluate_digest_forecasts(
    forecasts: list[dict],
    historical_fn: HistoricalPriceFn,
    current_fn: CurrentPriceFn,
) -> list[PostMortemEntry]:
    """Превращает list[forecast] (от DigestParser) в list[PostMortemEntry].

    Direction-forecasts оцениваются.  Price-forecasts пропускаются (это
    задача auto_tracker.AUTO_TRACK.md, а не post-mortem direction-теста).
    """
    entries: list[PostMortemEntry] = []
    for f in forecasts:
        if f.get("forecast_type") != "direction":
            continue

        raw_asset = str(f.get("asset", "")).strip()
        if not raw_asset:
            continue

        direction_raw = str(f.get("forecast", ""))
        direction = normalize_direction(direction_raw)
        forecast_date = str(f.get("date", ""))

        # VERDICT — глобальный, без актива.  Используем BTC как прокси.
        price_asset = VERDICT_PROXY_ASSET if raw_asset.upper() == "VERDICT" else raw_asset

        entry_price, eval_price = await _get_prices_for_asset(
            price_asset, forecast_date, historical_fn, current_fn
        )

        return_pct: Optional[float] = None
        if entry_price is not None and eval_price is not None and entry_price > 0:
            return_pct = (eval_price - entry_price) / entry_price * 100.0

        outcome = classify_outcome(direction, return_pct)
        explanation = explain_call(raw_asset, direction, return_pct, outcome)

        entries.append(
            PostMortemEntry(
                asset=raw_asset,
                direction=direction,
                forecast_date=forecast_date,
                entry_price=entry_price,
                eval_price=eval_price,
                return_pct=return_pct,
                outcome=outcome,
                explanation=explanation,
            )
        )
    return entries


# ─── DB WRITE-BACK ───────────────────────────────────────────────────────────


async def write_outcomes_back(
    entries: list[PostMortemEntry],
    *,
    db_path: Optional[str] = None,
) -> int:
    """Пишет каждый entry в `predictions` как post-mortem label.

    Существующий код калибровки (`core/calibration.link_provenance_outcomes`)
    делает fuzzy-join по asset+direction+time, поэтому каждый label виден
    калибровке сразу после записи.

    Дополнительно: если найдена provenance-запись с тем же asset+direction
    в окне ±2ч от digest_date — линкуем `provenance.prediction_id` на свежий
    prediction id (как делает `freeze_*` функции через `link_prediction`).
    Это даёт точный join вместо fuzzy и заполняет `entry.provenance_id`.

    Возвращает количество записанных prediction'ов.
    """
    path = db_path or DB_PATH
    written = 0

    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        for e in entries:
            if e.outcome == "no_data":
                # Не записываем — нет полезного label.
                continue

            result = outcome_to_prediction_result(e.outcome)
            direction_norm = direction_to_canonical_long_short(e.direction)
            pnl = float(e.return_pct) if e.return_pct is not None else 0.0
            entry_price = float(e.entry_price) if e.entry_price is not None else 0.0
            eval_price = float(e.eval_price) if e.eval_price is not None else 0.0

            forecast_dt = _parse_digest_datetime(e.forecast_date) or datetime.now()
            created_at_iso = forecast_dt.strftime("%Y-%m-%d %H:%M:%S")

            cursor = await db.execute(
                """
                INSERT INTO predictions (
                    created_at, asset, direction, entry_price,
                    target_price, stop_loss, timeframe, source_news,
                    result, result_price, result_at, pnl_pct,
                    prediction_type, forecast, fact, report_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?)
                """,
                (
                    created_at_iso,
                    e.asset,
                    direction_norm,
                    entry_price,
                    eval_price,
                    entry_price,
                    "24h",
                    "post-mortem (auto-labelled)"[:500],
                    result,
                    eval_price,
                    pnl,
                    "daily_digest",
                    e.direction,
                    f"{e.return_pct:+.2f}%" if e.return_pct is not None else "n/a",
                    "post_mortem",
                ),
            )
            pred_id = cursor.lastrowid
            e.prediction_id = pred_id

            # Линкуем decision_provenance.prediction_id если совпадает asset+direction
            # в окне.  Используем те же 2ч fuzzy-окно, что и калибровка.
            prov_id = await _find_unlinked_provenance(
                db,
                asset=e.asset,
                direction=direction_norm,
                created_at=created_at_iso,
                window_hours=2,
            )
            if prov_id is not None and pred_id is not None:
                await db.execute(
                    "UPDATE decision_provenance SET prediction_id = ? WHERE id = ?",
                    (pred_id, prov_id),
                )
                e.provenance_id = prov_id

            written += 1

        await db.commit()

    return written


async def _find_unlinked_provenance(
    db: aiosqlite.Connection,
    *,
    asset: str,
    direction: str,
    created_at: str,
    window_hours: int,
) -> Optional[int]:
    """Ищет provenance-запись с тем же asset+direction в окне ±window_hours
    у которой ещё нет prediction_id.  Возвращает id или None."""
    if direction not in ("LONG", "SHORT"):
        return None
    query = """
        SELECT id FROM decision_provenance
         WHERE asset = ?
           AND UPPER(direction) LIKE ?
           AND prediction_id IS NULL
           AND ABS((julianday(created_at) - julianday(?)) * 24) <= ?
      ORDER BY ABS((julianday(created_at) - julianday(?)) * 24) ASC
         LIMIT 1
    """
    like = f"%{direction}%"
    async with db.execute(
        query, (asset, like, created_at, float(window_hours), created_at)
    ) as cur:
        row = await cur.fetchone()
        if row is None:
            return None
        return int(row[0])


# ─── HIGH-LEVEL ENTRY ────────────────────────────────────────────────────────


async def run_post_mortem(
    target_date: Optional[str] = None,
    *,
    digest_cache_text: Optional[str] = None,
    historical_fn: Optional[HistoricalPriceFn] = None,
    current_fn: Optional[CurrentPriceFn] = None,
    now: Optional[datetime] = None,
    write_db: bool = True,
    db_path: Optional[str] = None,
) -> Optional[PostMortemReport]:
    """End-to-end оркестрация.

    Все зависимости injectable — это нужно для тестов.  В проде scheduler
    подсовывает `digest_cache_text` = чтение DIGEST_CACHE.md и реальные
    `historical_fn` / `current_fn` от `auto_tracker.PriceFetcher`.

    Возвращает PostMortemReport или None если нет подходящего дайджеста.
    """
    if digest_cache_text is None:
        try:
            cache_path = os.path.join(os.path.dirname(__file__), "..", "DIGEST_CACHE.md")
            cache_path = os.path.abspath(cache_path)
            with open(cache_path, encoding="utf-8") as f:
                digest_cache_text = f.read()
        except Exception as exc:
            logger.warning("post_mortem: не смог прочитать DIGEST_CACHE.md: %s", exc)
            return None

    # DigestParser — ленивый импорт, чтобы тесты не тянули yfinance.
    from auto_tracker import DigestParser

    digests = DigestParser.extract_all_digests(digest_cache_text)
    target_digest = pick_target_digest(
        digests, now=now, target_date=target_date,
    )
    if target_digest is None:
        logger.info("post_mortem: подходящий дайджест не найден (target_date=%s)", target_date)
        return None

    forecasts = DigestParser.extract_forecasts(target_digest)

    if historical_fn is None or current_fn is None:
        try:
            from auto_tracker import PriceDB, PriceFetcher
            price_db = PriceDB(db_path or DB_PATH)
            fetcher = PriceFetcher(price_db)

            async def _hist(ticker: str, date: str) -> Optional[float]:
                rec = await fetcher.get_historical_price(ticker, date)
                if rec and isinstance(rec, dict):
                    val = rec.get("price")
                    return float(val) if val is not None else None
                return None

            async def _cur(ticker: str) -> Optional[float]:
                rec = await fetcher.get_current_price(ticker)
                if rec and isinstance(rec, dict):
                    val = rec.get("price")
                    return float(val) if val is not None else None
                return None

            historical_fn = historical_fn or _hist
            current_fn = current_fn or _cur
        except Exception as exc:
            logger.warning("post_mortem: PriceFetcher unavailable: %s", exc)
            return None

    entries = await evaluate_digest_forecasts(forecasts, historical_fn, current_fn)

    if write_db and entries:
        try:
            await write_outcomes_back(entries, db_path=db_path)
        except Exception as exc:
            logger.exception("post_mortem: write_outcomes_back failed: %s", exc)

    return PostMortemReport(
        digest_date=target_digest.get("date", ""),
        horizon_hours=DEFAULT_HORIZON_HOURS,
        entries=entries,
    )


# ─── FORMATTERS ──────────────────────────────────────────────────────────────


def _outcome_emoji(outcome: str) -> str:
    return {
        "hit": "✅",
        "miss": "❌",
        "flat": "⚪",
        "neutral_correct": "✅",
        "neutral_missed": "❌",
        "no_data": "⚠️",
    }.get(outcome, "•")


def format_telegram(report: PostMortemReport, *, limit: int = 12) -> str:
    """Короткий формат для /postmortem.  Markdown-safe."""
    if not report.entries:
        return (
            f"🔬 *Post-mortem дайджеста {report.digest_date}*\n\n"
            f"Парсер не нашёл direction-прогнозов в этом дайджесте."
        )

    s = report.stats
    lines = [
        f"🔬 *Post-mortem дайджеста {report.digest_date}*",
        f"⏱ Горизонт: {report.horizon_hours}ч",
        "",
    ]

    if s["hit_rate"] is not None:
        wins, losses = s["wins"], s["losses"]
        lines.append(
            f"🎯 Hit-rate: *{s['hit_rate'] * 100:.1f}%* "
            f"({wins} верных / {wins + losses} разрешённых)"
        )
    else:
        lines.append("🎯 Hit-rate: — (мало разрешённых исходов)")

    if s["flat"]:
        lines.append(f"⚪ Flat: {s['flat']} (рынок не подтвердил)")
    if s["no_data"]:
        lines.append(f"⚠️ No-data: {s['no_data']}")
    lines.append("")

    lines.append("*Детали:*")
    for e in report.entries[:limit]:
        emoji = _outcome_emoji(e.outcome)
        move = "—" if e.return_pct is None else f"{e.return_pct:+.2f}%"
        lines.append(f"{emoji} *{e.asset}* {e.direction} → {move}")

    if len(report.entries) > limit:
        lines.append(f"…+{len(report.entries) - limit} ещё")

    lines.append("")
    lines.append("Labels записаны в `predictions` (`prediction_type='daily_digest'`).")
    lines.append("Откроется в /calibration после накопления ≥20 разрешённых пунктов.")
    return "\n".join(lines)


def format_markdown(report: PostMortemReport) -> str:
    """Расширенный markdown для POSTMORTEM.md в репо."""
    s = report.stats
    out: list[str] = []
    out.append(f"## 🔬 Post-mortem — дайджест {report.digest_date}")
    out.append("")
    out.append(f"- Горизонт: {report.horizon_hours}ч")
    out.append(f"- Всего пунктов: {s['total']}")
    out.append(f"- Разрешённых: {s['resolved']} (hit={s['counts']['hit']}, "
               f"miss={s['counts']['miss']}, neutral_correct={s['counts']['neutral_correct']}, "
               f"neutral_missed={s['counts']['neutral_missed']})")
    out.append(f"- Flat: {s['flat']}")
    out.append(f"- No-data: {s['no_data']}")
    if s["hit_rate"] is not None:
        out.append(f"- **Hit-rate: {s['hit_rate'] * 100:.1f}%**")
    out.append("")
    out.append("| Asset | Direction | Entry | Eval | Δ% | Outcome | Объяснение |")
    out.append("|---|---|---|---|---|---|---|")
    for e in report.entries:
        ep = f"{e.entry_price:.4g}" if e.entry_price is not None else "—"
        xp = f"{e.eval_price:.4g}" if e.eval_price is not None else "—"
        rp = f"{e.return_pct:+.2f}%" if e.return_pct is not None else "—"
        emoji = _outcome_emoji(e.outcome)
        out.append(
            f"| {e.asset} | {e.direction} | {ep} | {xp} | {rp} | "
            f"{emoji} {e.outcome} | {e.explanation} |"
        )
    out.append("")
    out.append("> Источник: `core/post_mortem.py`. Метки feed в `predictions` →"
               " `/calibration` подхватит автоматически.")
    return "\n".join(out)


# ─── PUBLIC API ──────────────────────────────────────────────────────────────


__all__ = [
    "PostMortemEntry",
    "PostMortemReport",
    "FLAT_THRESHOLD_PCT",
    "DEFAULT_HORIZON_HOURS",
    "classify_outcome",
    "outcome_to_prediction_result",
    "normalize_direction",
    "direction_to_canonical_long_short",
    "explain_call",
    "compute_stats",
    "pick_target_digest",
    "evaluate_digest_forecasts",
    "write_outcomes_back",
    "run_post_mortem",
    "format_telegram",
    "format_markdown",
]


# ─── async-safe sync wrapper for occasional use ──────────────────────────────


def _sync_run_post_mortem(*args: Any, **kwargs: Any) -> Optional[PostMortemReport]:
    """Дев-утилита: позволяет запустить post-mortem из синхронного контекста.
    Используется только в CLI-смоктестах, не в проде."""
    return asyncio.run(run_post_mortem(*args, **kwargs))
