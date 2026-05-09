"""
Централизованные версии промптов/пайплайна для аудита дайджестов.
Меняйте строки версий при существенных правках промптов или оркестратора.
"""

from __future__ import annotations

# Пайплайн генерации дайджеста (кодовые пути)
ANALYSIS_PIPELINE_ID = "analysis_service.run_full_analysis"
DEBATE_ORCHESTRATOR_ID = "agents.DebateOrchestrator"

# Логические версии (ручная метка; синхронизируйте с коммитом при релизе)
# 2026-05-09.1 — multi-horizon (intraday/swing/position): SYNTH_BASE_SYSTEM +
# synth_overlay, ПРАВИЛО ЧЕСТНОСТИ в Bull/Bear, инвалидация-якорь, плагин
# Verifier Step 5, hard-guard plan-geometry + code-side stop-factor override.
DIGEST_PIPELINE_VERSION = "2026-05-09.1"
SENTIMENT_MODULE_VERSION = "sentiment.analyze_and_filter_async/v1"
SANITIZER_VERSION = "report_sanitizer.sanitize_full_report/v1"
HORIZONS_MODULE_VERSION = "core.horizons/v1"


def get_digest_prompt_manifest(horizon: str | None = None) -> dict:
    """Снимок версий, сохраняемый вместе с daily_context.

    Если передан `horizon` — попадёт в манифест, чтобы при разборе расхождений
    результата (intraday/swing/position) можно было точно сказать какой
    горизонт давал аналитика.
    """
    manifest = {
        "digest_pipeline_version": DIGEST_PIPELINE_VERSION,
        "analysis_pipeline": ANALYSIS_PIPELINE_ID,
        "debate_orchestrator": DEBATE_ORCHESTRATOR_ID,
        "sentiment": SENTIMENT_MODULE_VERSION,
        "sanitizer": SANITIZER_VERSION,
        "horizons_module": HORIZONS_MODULE_VERSION,
    }
    if horizon:
        manifest["horizon"] = horizon
    return manifest
