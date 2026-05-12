"""
smart_money_alert.py — Алерт при конвергенции smart-money сигналов.

Идея: regular MARKET SIGNALS digest приходит раз в 2 часа независимо от того,
есть ли что показать. Этот модуль шлёт ОТДЕЛЬНЫЙ prominent алерт только
когда ≥2 институциональных индикаторов (Top-trader L/S, Coinbase Premium,
CME Basis, Funding dispersion) одновременно показывают одно направление
с заметной силой — то есть «много крупных трейдеров почти в одно время
зашли в одну сторону».

Источник данных — `market_indicators.smart_money.fetch_smart_money_signals`
+ scoring из `smart_money_score_contribution`.

Анти-спам:
- Не шлём одно и то же направление чаще раз в COOLDOWN_HOURS
- Шлём сразу если direction сменился (например, LONG → SHORT)
- Шлём сразу если score стал заметно сильнее (delta ≥ STRENGTH_BUMP)

Алерт — это информация, НЕ торговый сигнал. Решение за пользователем.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from market_indicators.smart_money import (
    SmartMoneySignals,
    fetch_smart_money_signals,
    smart_money_score_contribution,
)

logger = logging.getLogger(__name__)

# Минимум баллов чтобы считать конвергенцию состоявшейся (range примерно ±7)
MIN_SCORE = 3

# Минимум независимых категорий-сигналов в одну сторону
MIN_REASONS = 2

# Через сколько часов разрешаем повторно слать тот же direction
COOLDOWN_HOURS = 6

# Если score прибавил на столько vs предыдущего — игнорим cooldown
STRENGTH_BUMP = 2


def _format_alert(direction: str, score: int, reasons: list[str], signals: SmartMoneySignals) -> str:
    """Текст алерта для Telegram (Markdown)."""
    if direction == "LONG":
        emoji = "🟢"
        title = "топ-трейдеры и институционалы синхронно идут в *LONG*"
    else:
        emoji = "🔴"
        title = "топ-трейдеры и институционалы синхронно идут в *SHORT*"

    lines = [
        f"{emoji} *SMART-MONEY CONVERGENCE*",
        f"_{datetime.now().strftime('%d.%m.%Y %H:%M UTC')}_",
        "",
        f"BTC: {title}",
        "",
        "📊 *Совпавшие индикаторы:*",
    ]
    for r in reasons:
        lines.append(f"• {r}")

    lines.append("")
    lines.append(f"📈 *Score:* {score:+d} (порог: ≥{MIN_SCORE} или ≤−{MIN_SCORE})")

    lines.extend(
        [
            "",
            "💡 _Это информация, а не торговый сигнал. /daily для полного плана. "
            "Если вердикт /daily — NEUTRAL, входить или нет — твоё решение._",
            "",
            "⚠️ _DYOR. Не финансовый совет._",
        ]
    )
    return "\n".join(lines)


def _evaluate_convergence(
    score: int, bullish: list[str], bearish: list[str]
) -> tuple[Optional[str], list[str]]:
    """Решает, есть ли конвергенция, и в какую сторону.

    Returns: (direction, reasons) или (None, []).
    """
    if score >= MIN_SCORE and len(bullish) >= MIN_REASONS:
        return "LONG", bullish
    if score <= -MIN_SCORE and len(bearish) >= MIN_REASONS:
        return "SHORT", bearish
    return None, []


class SmartMoneyAlertSystem:
    """Отслеживает конвергенцию smart-money сигналов и шлёт алерты подписчикам."""

    def __init__(self, bot):
        self.bot = bot
        self._last_direction: Optional[str] = None
        self._last_score: int = 0
        self._last_time: Optional[datetime] = None

    def _should_send(self, direction: str, score: int) -> bool:
        now = datetime.now()

        # Смена направления — всегда шлём
        if direction != self._last_direction:
            return True

        # Резкое усиление того же направления — шлём
        if abs(score) >= abs(self._last_score) + STRENGTH_BUMP:
            return True

        # Иначе — соблюдаем cooldown
        if self._last_time is None:
            return True
        hours_passed = (now - self._last_time).total_seconds() / 3600
        return hours_passed >= COOLDOWN_HOURS

    async def check_and_alert(self, subscribers: list[dict]) -> int:
        """Один цикл проверки: fetch → scoring → send (если есть конвергенция).

        Возвращает число отправленных сообщений (0 если не сработало).
        """
        if not subscribers:
            return 0

        try:
            signals = await fetch_smart_money_signals()
        except Exception as e:
            logger.warning(f"smart-money fetch error: {e}")
            return 0

        score, bullish, bearish = smart_money_score_contribution(signals)
        direction, reasons = _evaluate_convergence(score, bullish, bearish)

        logger.info(
            "smart-money check: score=%+d bull=%d bear=%d → %s",
            score, len(bullish), len(bearish), direction or "—",
        )

        if direction is None:
            return 0

        if not self._should_send(direction, score):
            logger.info(
                "smart-money convergence %s suppressed (cooldown / no strength bump)",
                direction,
            )
            return 0

        text = _format_alert(direction, score, reasons, signals)
        sent = 0
        for user in subscribers:
            try:
                await self.bot.send_message(
                    user["user_id"],
                    text,
                    parse_mode="Markdown",
                )
                sent += 1
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.warning(f"smart-money alert send error user {user['user_id']}: {e}")

        self._last_direction = direction
        self._last_score = score
        self._last_time = datetime.now()
        logger.info(f"✅ smart-money convergence alert sent to {sent} subscribers")
        return sent
