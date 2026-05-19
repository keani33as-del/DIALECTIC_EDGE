"""Decision provenance — замораживает каждое торговое решение с feature-snapshot.

Зачем: когда бот говорит «SHORT SOL, score=72, confidence=75%», через месяц
мы должны уметь ответить на вопрос «какие именно 11 чисел привели к этому
решению?» — без реконструкции данных, без «ну наверное было так».

Две точки записи:
  1. `freeze_scorer_decision()` — вызывается из `rank_signals()` (signal_scorer.py)
     после выбора top-setup. Фиксирует: все raw-фичи актива (prices dict),
     ScoreBreakdown (trend/complexity/VRT/Markov/tradeable), SL/TP/σ̂, direction.

  2. `freeze_pick_best_decision()` — вызывается из `pick_best_signal()` (signals.py)
     после выбора лучшего R-score сигнала. Фиксирует: все кандидаты-сигналы,
     R-score компоненты, bias_map, quant/funding/verdict данные.

Каждая запись получает git SHA (если .git доступен), версию scorer'а,
и JSON-snapshot входных данных. Ничего не модифицирует в существующей логике —
только пишет в таблицу `decision_provenance`.

Replay: `get_provenance(id)` возвращает полный snapshot для отладки.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Optional

import aiosqlite

from config import DB_PATH

logger = logging.getLogger(__name__)

# Версия схемы provenance — бампать при изменении формата features/weights JSON.
PROVENANCE_SCHEMA_VERSION = "1.0"

# Максимальный размер JSON-полей (защита от OOM на гигантских prices dict).
_MAX_JSON_CHARS = 64_000


def _git_sha() -> str:
    """Возвращает короткий git SHA текущего коммита, или 'unknown'."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _safe_json(obj: object) -> str:
    """Сериализует объект в JSON, обрезая до _MAX_JSON_CHARS."""
    try:
        raw = json.dumps(obj, ensure_ascii=False, default=str)
        if len(raw) > _MAX_JSON_CHARS:
            raw = raw[:_MAX_JSON_CHARS] + '"…[truncated]"}'
        return raw
    except Exception as exc:
        return json.dumps({"_serialization_error": str(exc)})


# ─── CREATE TABLE ────────────────────────────────────────────────────────────

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS decision_provenance (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    decision_type   TEXT    NOT NULL,
    asset           TEXT    NOT NULL,
    direction       TEXT    NOT NULL,
    score           INTEGER,
    entry_price     REAL,
    stop_loss       REAL,
    take_profit     REAL,
    sigma_1d_pct    REAL,
    features_json   TEXT    NOT NULL,
    weights_json    TEXT    NOT NULL,
    signals_json    TEXT,
    regime_json     TEXT,
    code_version    TEXT,
    schema_version  TEXT    NOT NULL DEFAULT '1.0',
    prediction_id   INTEGER,
    trade_log_id    INTEGER
)
"""

CREATE_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_prov_asset     ON decision_provenance (asset)",
    "CREATE INDEX IF NOT EXISTS idx_prov_created    ON decision_provenance (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_prov_direction  ON decision_provenance (direction)",
    "CREATE INDEX IF NOT EXISTS idx_prov_type       ON decision_provenance (decision_type)",
]


async def ensure_table() -> None:
    """Создаёт таблицу и индексы если не существуют. Идемпотентно."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE_SQL)
        for idx_sql in CREATE_INDEX_SQL:
            await db.execute(idx_sql)
        await db.commit()


# ─── FREEZE (запись) ─────────────────────────────────────────────────────────

async def freeze_scorer_decision(
    asset: str,
    direction: str,
    score: int,
    entry_price: Optional[float],
    stop_loss: Optional[float],
    take_profit: Optional[float],
    sigma_1d_pct: Optional[float],
    features: dict,
    weights: dict,
    regime: Optional[dict] = None,
) -> int:
    """Замораживает решение signal_scorer (детерминированный путь).

    Parameters
    ----------
    asset : str
        Тикер (BTC, ETH, SOL, ...).
    direction : str
        LONG / SHORT / NONE.
    score : int
        Composite trade-score 0-100.
    entry_price, stop_loss, take_profit, sigma_1d_pct : Optional[float]
        Уровни из SignalSetup (None если setup не построен).
    features : dict
        Полный prices[asset] dict на момент решения.
    weights : dict
        ScoreBreakdown как dict (trend_alignment, complexity_hint, ...).
    regime : dict, optional
        Состояние режима (trend, complexity_hint, markov_state, hurst, ...).

    Returns
    -------
    int
        ID записи в decision_provenance.
    """
    code_ver = _git_sha()

    regime_data = regime or _extract_regime(features)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO decision_provenance (
                decision_type, asset, direction, score,
                entry_price, stop_loss, take_profit, sigma_1d_pct,
                features_json, weights_json, regime_json,
                code_version, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "signal_scorer",
                asset,
                direction,
                score,
                entry_price,
                stop_loss,
                take_profit,
                sigma_1d_pct,
                _safe_json(features),
                _safe_json(weights),
                _safe_json(regime_data),
                code_ver,
                PROVENANCE_SCHEMA_VERSION,
            ),
        )
        await db.commit()
        prov_id = int(cur.lastrowid)
    logger.info(
        "provenance frozen: id=%d type=signal_scorer asset=%s dir=%s score=%d",
        prov_id, asset, direction, score,
    )
    return prov_id


async def freeze_pick_best_decision(
    best_signal: Optional[dict],
    all_signals: list,
    binance_data: dict,
    verdict: Optional[dict],
    bias_map: Optional[dict] = None,
) -> int:
    """Замораживает решение pick_best_signal (R-score путь).

    Parameters
    ----------
    best_signal : dict or None
        Выбранный лучший сигнал (или None если ничего не прошло порог).
    all_signals : list
        Все кандидаты-сигналы из analyze_signals().
    binance_data : dict
        Полный dict данных Binance/Bybit на момент решения.
    verdict : dict or None
        Вердикт из DIGEST_CACHE.
    bias_map : dict or None
        Результат build_signal_bias_map().

    Returns
    -------
    int
        ID записи в decision_provenance.
    """
    code_ver = _git_sha()

    asset = (best_signal or {}).get("symbol", "NONE")
    if asset.endswith("USDT"):
        asset = asset[:-4]

    direction = (best_signal or {}).get("direction", "NONE")
    r_score = int((best_signal or {}).get("r_score", 0))
    confidence = (best_signal or {}).get("confidence", 0)

    features = {
        "binance_data_snapshot": _compact_binance(binance_data),
        "verdict": verdict,
    }
    weights = {
        "r_score": r_score,
        "confidence": confidence,
        "r_score_components": (best_signal or {}).get("r_score_components", {}),
        "bias_map": _compact_bias_map(bias_map) if bias_map else None,
    }
    signals_data = [
        {
            "symbol": s.get("symbol"),
            "direction": s.get("direction"),
            "type": s.get("type"),
            "confidence": s.get("confidence"),
            "r_score": s.get("r_score"),
        }
        for s in all_signals
    ]

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO decision_provenance (
                decision_type, asset, direction, score,
                features_json, weights_json, signals_json,
                code_version, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "pick_best",
                asset,
                direction,
                r_score,
                _safe_json(features),
                _safe_json(weights),
                _safe_json(signals_data),
                code_ver,
                PROVENANCE_SCHEMA_VERSION,
            ),
        )
        await db.commit()
        prov_id = int(cur.lastrowid)
    logger.info(
        "provenance frozen: id=%d type=pick_best asset=%s dir=%s r_score=%d",
        prov_id, asset, direction, r_score,
    )
    return prov_id


# ─── READ (replay / debug) ──────────────────────────────────────────────────

async def get_provenance(provenance_id: int) -> Optional[dict]:
    """Возвращает полную запись provenance по ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM decision_provenance WHERE id = ?", (provenance_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return _decode_row(row)


async def get_recent_provenances(
    asset: Optional[str] = None,
    decision_type: Optional[str] = None,
    limit: int = 10,
) -> list[dict]:
    """Возвращает последние записи provenance с опциональной фильтрацией."""
    conditions = []
    params: list = []

    if asset:
        conditions.append("asset = ?")
        params.append(asset)
    if decision_type:
        conditions.append("decision_type = ?")
        params.append(decision_type)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    query = f"""
        SELECT * FROM decision_provenance
        {where}
        ORDER BY id DESC
        LIMIT ?
    """
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [_decode_row(row) for row in rows]


async def link_prediction(provenance_id: int, prediction_id: int) -> None:
    """Связывает provenance запись с prediction (после создания prediction)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE decision_provenance SET prediction_id = ? WHERE id = ?",
            (prediction_id, provenance_id),
        )
        await db.commit()


async def link_trade_log(provenance_id: int, trade_log_id: int) -> None:
    """Связывает provenance запись с trade_decision_log."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE decision_provenance SET trade_log_id = ? WHERE id = ?",
            (trade_log_id, provenance_id),
        )
        await db.commit()


# ─── FORMAT (для Telegram-вывода) ────────────────────────────────────────────

def format_provenance_telegram(prov: dict) -> str:
    """Форматирует provenance запись для Telegram-сообщения."""
    lines = []
    lines.append(f"🔍 Provenance #{prov['id']}")
    lines.append(f"📅 {prov['created_at']}")
    lines.append(f"📦 Type: {prov['decision_type']}")
    lines.append(f"💰 {prov['asset']} → {prov['direction']}")

    if prov.get("score"):
        lines.append(f"📊 Score: {prov['score']}/100")

    if prov.get("entry_price"):
        lines.append(f"🎯 Entry: ${prov['entry_price']:,.2f}")
    if prov.get("stop_loss"):
        lines.append(f"🛑 SL: ${prov['stop_loss']:,.2f}")
    if prov.get("take_profit"):
        lines.append(f"✅ TP: ${prov['take_profit']:,.2f}")
    if prov.get("sigma_1d_pct"):
        lines.append(f"📈 σ̂: {prov['sigma_1d_pct']:.2f}%")

    weights = prov.get("weights_json") or {}
    if isinstance(weights, dict):
        if "trend_alignment" in weights:
            lines.append("")
            lines.append("⚖️ Score breakdown:")
            for k, v in weights.items():
                if isinstance(v, (int, float)):
                    lines.append(f"  • {k}: {v}")
        elif "r_score_components" in weights:
            comps = weights.get("r_score_components", {})
            if comps:
                lines.append("")
                lines.append("⚖️ R-score components:")
                for k, v in comps.items():
                    if isinstance(v, (int, float)):
                        lines.append(f"  • {k}: {v:+.1f}")

    regime = prov.get("regime_json") or {}
    if isinstance(regime, dict) and regime:
        lines.append("")
        lines.append("🌡️ Regime:")
        for k, v in regime.items():
            lines.append(f"  • {k}: {v}")

    lines.append(f"\n🔧 Code: {prov.get('code_version', '?')}")
    lines.append(f"📋 Schema: {prov.get('schema_version', '?')}")

    return "\n".join(lines)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _extract_regime(features: dict) -> dict:
    """Извлекает regime-данные из prices dict актива."""
    regime_keys = [
        "trend", "complexity_hint", "hurst", "tradeable_score",
        "vrt_random_walk", "vrt_ratio", "markov_state", "markov_next_probs",
        "markov_expected_duration", "vol_sigma_1d_pct",
        "vol_sigma_annual_pct", "ma50", "ma200", "price",
    ]
    return {k: features.get(k) for k in regime_keys if features.get(k) is not None}


def _compact_binance(binance_data: dict) -> dict:
    """Компактная версия binance_data (убираем тяжёлые поля)."""
    compact = {}
    for sym, data in binance_data.items():
        if not isinstance(data, dict):
            continue
        compact[sym] = {
            "last_price": data.get("last_price"),
            "price_change": data.get("price_change"),
            "funding_rate": data.get("funding_rate"),
            "funding_direction": data.get("funding_direction"),
            "long": data.get("long"),
            "short": data.get("short"),
            "dominant": data.get("dominant"),
            "quant_verdict": data.get("quant_verdict"),
            "quant_confidence": data.get("quant_confidence"),
        }
    return compact


def _compact_bias_map(bias_map: dict) -> dict:
    """Компактная версия bias_map для JSON-хранения."""
    compact = {}
    for sym, bias in bias_map.items():
        if isinstance(bias, dict):
            compact[sym] = bias
        elif hasattr(bias, "__dict__"):
            compact[sym] = {
                k: v for k, v in vars(bias).items()
                if not k.startswith("_")
            }
        else:
            compact[sym] = str(bias)
    return compact


def _decode_row(row) -> dict:
    """Декодирует sqlite row в dict с JSON-полями."""
    try:
        data = dict(row)
    except Exception:
        data = {k: row[k] for k in row.keys()}

    for json_key in ("features_json", "weights_json", "signals_json", "regime_json"):
        raw = data.get(json_key)
        if isinstance(raw, str):
            try:
                data[json_key] = json.loads(raw)
            except Exception:
                pass
    return data
