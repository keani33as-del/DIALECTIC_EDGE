"""
core/sizing_state.py — Conservative sizing для первых N сделок после обновления промптов.

Pre-live-hardening, Requirement B:
Новые промпты (PR #30) не имеют live track-record. Первые 3 actionable-сделки
(LONG/SHORT) автоматически получают signal_pct × 0.5. После 3 сделок — полный размер.

Состояние хранится в JSON-файле (персистентно между рестартами).

Env-переменные:
  SIZING_STATE_PATH — путь к state-файлу (default: .sizing_state.json в корне проекта)
  DISABLE_SIZING_BAKE_IN=1 — полностью отключает conservative sizing (для CI/тестов)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_STATE_FILE = Path(os.getenv("SIZING_STATE_PATH", ".sizing_state.json"))
_PROMPT_BAKE_VERSION = "pr30_prompts_v1"  # bump при значительном изменении промптов
_BAKE_TRADES = 3  # первые N actionable-сделок идут в половинном размере


def _load() -> dict:
    """Загрузить state из файла. Если файла нет — пустой dict."""
    if not _STATE_FILE.exists():
        return {}
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[SIZING] Не удалось прочитать state: {e}")
        return {}


def _save(state: dict) -> None:
    """Сохранить state в файл."""
    try:
        _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[SIZING] Не удалось сохранить state: {e}")


def get_multiplier() -> float:
    """Возвращает 0.5 если bake-in активен, иначе 1.0."""
    if os.getenv("DISABLE_SIZING_BAKE_IN", "").strip() == "1":
        return 1.0
    state = _load()
    if state.get("prompt_version") != _PROMPT_BAKE_VERSION:
        return 0.5
    if state.get("trades_since_bake", 0) < _BAKE_TRADES:
        return 0.5
    return 1.0


def record_actionable_trade() -> int:
    """Инкрементирует счётчик actionable-сделок. Возвращает новый count."""
    if os.getenv("DISABLE_SIZING_BAKE_IN", "").strip() == "1":
        return _BAKE_TRADES  # не считаем
    state = _load()
    if state.get("prompt_version") != _PROMPT_BAKE_VERSION:
        state = {"prompt_version": _PROMPT_BAKE_VERSION, "trades_since_bake": 0}
    state["trades_since_bake"] = state.get("trades_since_bake", 0) + 1
    _save(state)
    return state["trades_since_bake"]


def bake_in_badge() -> str:
    """Текст плашки для дайджеста. Пустая строка если bake-in не активен."""
    if os.getenv("DISABLE_SIZING_BAKE_IN", "").strip() == "1":
        return ""
    state = _load()
    version = state.get("prompt_version", "")
    trades = state.get("trades_since_bake", 0) if version == _PROMPT_BAKE_VERSION else 0
    remaining = _BAKE_TRADES - trades
    if version != _PROMPT_BAKE_VERSION or remaining > 0:
        return (
            f"⚠️ CONSERVATIVE SIZING ACTIVE ({trades}/{_BAKE_TRADES} trades before full size) "
            "— new prompts bake-in"
        )
    return ""


def is_bake_in_active() -> bool:
    """True если conservative sizing сейчас активен."""
    return get_multiplier() < 1.0
