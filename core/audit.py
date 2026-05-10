"""
core/audit.py — AI self-audit. LLM смотрит на закрытые сделки и пишет
структурный «отчёт о работе» в стиле performance review.

Pitch: «AI которая учится на своих ошибках». Это разница между retail-системой
и систематическим фондом — пост-фактум анализ, выявление повторяющихся ошибок,
корректировка параметров.

API:
    build_audit_prompt(trades, risk_summary, period="неделю") -> str
    parse_recent_trades_from_md(markdown_path, days=7) -> list[dict]
    format_audit_for_telegram(audit_text) -> str
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def parse_recent_trades_from_md(markdown_path: str, days: int = 7) -> list[dict]:
    """Парсит таблицу `## 📋 История сделок` из BACKTEST.md за N дней.
    
    Формат строки:
    | 2026-05-08 | BNB | BUY | $637 | $637 | $-0 | -0.0% |
    """
    trades: list[dict] = []
    p = Path(markdown_path)
    if not p.exists():
        return trades
    
    cutoff = datetime.utcnow().date() - timedelta(days=days)
    
    text = p.read_text(encoding="utf-8", errors="ignore")
    # Найдём блок таблицы
    section = re.search(r"## .*История сделок.*?\n(.*?)(?:\n##|\Z)", text, re.DOTALL)
    if not section:
        return trades
    
    body = section.group(1)
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|") or "----" in line or "Дата" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 7:
            continue
        try:
            date = datetime.strptime(cells[0], "%Y-%m-%d").date()
        except ValueError:
            continue
        if date < cutoff:
            continue
        try:
            entry = float(cells[3].replace("$", "").replace(",", ""))
            exit_p = float(cells[4].replace("$", "").replace(",", ""))
            pnl_usd = float(cells[5].replace("$", "").replace(",", ""))
            pnl_pct = float(cells[6].replace("%", ""))
        except (ValueError, IndexError):
            continue
        
        trades.append({
            "date": cells[0],
            "symbol": cells[1],
            "direction": cells[2],
            "entry": entry,
            "exit": exit_p,
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
        })
    
    return trades


def _trade_summary_table(trades: list[dict]) -> str:
    """Форматирует список сделок таблицей для prompt'а."""
    if not trades:
        return "_(нет закрытых сделок за период)_"
    lines = ["Дата       | Актив | Dir | Entry  | Exit   | PnL$    | PnL%"]
    for t in trades:
        lines.append(
            f"{t['date']:10s} | {t['symbol']:5s} | {t['direction']:3s} | "
            f"${t['entry']:7,.2f} | ${t['exit']:7,.2f} | "
            f"${t['pnl_usd']:+7.2f} | {t['pnl_pct']:+5.2f}%"
        )
    return "\n".join(lines)


def build_audit_prompt(
    trades: list[dict],
    risk_summary: dict | None = None,
    period: str = "неделю",
) -> str:
    """Собирает prompt для self-audit LLM-агента.
    
    Возвращает полный prompt включая system-инструкции.
    """
    if not trades:
        return (
            "Сделок за период нет. Ответь коротко: "
            "«📊 За {} закрытых сделок не было — анализировать нечего.»"
        ).format(period)
    
    total = len(trades)
    wins = sum(1 for t in trades if t["pnl_usd"] > 0)
    losses = sum(1 for t in trades if t["pnl_usd"] < 0)
    breakevens = total - wins - losses
    win_rate = (wins / total * 100) if total else 0
    
    total_pnl_usd = sum(t["pnl_usd"] for t in trades)
    total_pnl_pct = sum(t["pnl_pct"] for t in trades)
    avg_win = (sum(t["pnl_pct"] for t in trades if t["pnl_pct"] > 0) /
               max(wins, 1)) if wins else 0
    avg_loss = (sum(t["pnl_pct"] for t in trades if t["pnl_pct"] < 0) /
                max(losses, 1)) if losses else 0
    rr = (avg_win / abs(avg_loss)) if avg_loss else 0
    
    rs_lines = []
    if risk_summary:
        rs_lines.append("**Текущее состояние Risk Manager'а:**")
        rs_lines.append(f"- Capital: ${risk_summary.get('current_capital', 0):.2f}")
        rs_lines.append(f"- Peak: ${risk_summary.get('peak_capital', 0):.2f}")
        rs_lines.append(f"- Drawdown: {risk_summary.get('drawdown_pct', 0):.1f}%")
        rs_lines.append(f"- Win-rate (overall): {risk_summary.get('win_rate', 0):.1f}%")
        rs_lines.append(f"- Kelly suggestion: {risk_summary.get('kelly_pct', 0):.2f}%")
        rs_lines.append(f"- Kelly using history: {risk_summary.get('kelly_using_history', False)}")
    
    table = _trade_summary_table(trades)
    
    return f"""Ты — risk officer количественного фонда. Тебе дали отчёт за последнюю {period} закрытых сделок.

Твоя задача: написать **performance review** на 6-10 строк в формате:

1. **Общая оценка** (1 строка) — winning/losing/neutral период.
2. **Что работает** — 1-2 buy/sell-сигнала или режима которые сработали и почему.
3. **Что НЕ работает** — 1-2 конкретные ошибки/паттерна (например: "мы шортили в RISK_ON режиме", "стопы слишком тугие на BTC", "входим без подтверждения от smart-money").
4. **Конкретное действие на следующую неделю** — 1 правило которое стоит изменить (например: "не открывать BUY против отрицательного Coinbase Premium >0.3%", "поднять stop-loss до 1.5x ATR на SOL").

**Жёсткие требования:**
- Без воды и общих фраз ("следите за рынком", "будьте осторожны").
- Каждое утверждение подкреплено конкретной цифрой из таблицы или risk-state.
- На русском.
- Если win-rate < 50% — признай это и **не оправдывай**.

═══════════════════════════════════════
**Статистика за {period}:**
- Сделок: {total} ({wins} прибыльных, {losses} убыточных, {breakevens} на нуле)
- Win-rate: {win_rate:.1f}%
- Total PnL: ${total_pnl_usd:+.2f} ({total_pnl_pct:+.2f}%)
- Средний выигрыш: {avg_win:+.2f}%, средний убыток: {avg_loss:+.2f}%
- Win/Loss ratio: {rr:.2f}

{chr(10).join(rs_lines) if rs_lines else ""}

**Закрытые сделки:**
```
{table}
```
═══════════════════════════════════════

Performance review:"""


def format_audit_for_telegram(audit_text: str, trades_count: int, period: str) -> str:
    """Финальная обёртка для Telegram message с метаданными."""
    if not audit_text or not audit_text.strip():
        return f"📊 *AI Self-Audit ({period})*\n\nLLM не вернул анализ — попробуй позже."
    
    header = f"📊 *AI Self-Audit ({period}, {trades_count} сделок)*\n"
    header += "═" * 25 + "\n"
    return header + audit_text.strip()
