"""
weekly_report.py — Еженедельный отчёт точности агентов.

Каждое воскресенье бот сам себя проверяет:
- Сколько прогнозов за неделю
- Сколько оказались верными по направлению
- Средний P&L
- Где ошибались и почему

Это строит доверие через радикальную прозрачность.
"""

import logging
from datetime import datetime, timedelta
from database import DB_PATH
import aiosqlite

logger = logging.getLogger(__name__)


async def get_weekly_stats() -> dict:
    """Статистика за последние 7 дней."""
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Общая статистика за неделю
        async with db.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN result = 'win'  THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN result = 'pending' THEN 1 ELSE 0 END) as pending,
                AVG(CASE WHEN result IN ('win','loss') THEN pnl_pct END) as avg_pnl,
                MAX(pnl_pct) as best_pnl,
                MIN(pnl_pct) as worst_pnl
            FROM predictions
            WHERE created_at >= ?
        """, (week_ago,)) as cursor:
            stats = dict(await cursor.fetchone())

        # Детали прогнозов
        async with db.execute("""
            SELECT asset, direction, entry_price, result_price,
                   result, pnl_pct, created_at, source_news
            FROM predictions
            WHERE created_at >= ? AND result IN ('win', 'loss')
            ORDER BY pnl_pct DESC
        """, (week_ago,)) as cursor:
            details = [dict(r) for r in await cursor.fetchall()]

        # Фидбек за неделю
        async with db.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END) as positive
            FROM feedback
            WHERE created_at >= ?
        """, (week_ago,)) as cursor:
            feedback = dict(await cursor.fetchone())

        # Активные пользователи
        async with db.execute("""
            SELECT COUNT(DISTINCT user_id) as active
            FROM reports
            WHERE created_at >= ?
        """, (week_ago,)) as cursor:
            users = dict(await cursor.fetchone())

    return {
        "stats": stats,
        "details": details,
        "feedback": feedback,
        "users": users,
        "period": f"{(datetime.now()-timedelta(days=7)).strftime('%d.%m')} — {datetime.now().strftime('%d.%m.%Y')}"
    }


async def build_weekly_report() -> str:
    """Строит красивый еженедельный отчёт для пользователей."""
    data = await get_weekly_stats()
    stats = data["stats"]
    details = data["details"]
    feedback = data["feedback"]
    users = data["users"]

    total = stats.get("total") or 0
    wins = stats.get("wins") or 0
    losses = stats.get("losses") or 0
    pending = stats.get("pending") or 0
    avg_pnl = stats.get("avg_pnl") or 0
    best = stats.get("best_pnl") or 0
    worst = stats.get("worst_pnl") or 0

    finished = wins + losses
    winrate = (wins / finished * 100) if finished > 0 else 0

    # Оценка недели
    if winrate >= 65:
        week_grade = "🟢 Отличная неделя"
        grade_comment = "Агенты работали хорошо — большинство сигналов верные"
    elif winrate >= 50:
        week_grade = "🟡 Средняя неделя"
        grade_comment = "Половина прогнозов верна — обычный результат для неопределённого рынка"
    elif finished < 3:
        week_grade = "⚪ Мало данных"
        grade_comment = "Недостаточно завершённых прогнозов для оценки"
    else:
        week_grade = "🔴 Слабая неделя"
        grade_comment = "Больше ошибок чем обычно — анализируем причины"

    # Фидбек пользователей
    total_fb = feedback.get("total") or 0
    positive_fb = feedback.get("positive") or 0
    satisfaction = (positive_fb / total_fb * 100) if total_fb > 0 else 0

    lines = [
        f"📊 *ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ ТОЧНОСТИ*",
        f"📅 Период: {data['period']}",
        f"",
        f"{week_grade}",
        f"_{grade_comment}_",
        f"",
        f"{'─'*30}",
        f"",
        f"*🎯 Прогнозы агентов:*",
        f"• Всего за неделю: {total}",
        f"• Завершено: {finished} | ⏳ В процессе: {pending}",
    ]

    if finished > 0:
        wr_emoji = "🟢" if winrate >= 55 else "🟡" if winrate >= 45 else "🔴"
        pnl_emoji = "🟢" if avg_pnl >= 0 else "🔴"
        lines += [
            f"• Winrate: {wr_emoji} *{winrate:.0f}%* ({wins} верных / {losses} неверных)",
            f"• Средний P&L: {pnl_emoji} *{avg_pnl:+.1f}%*",
        ]
        if best:
            lines.append(f"• 🚀 Лучший сигнал: +{best:.1f}%")
        if worst and worst < 0:
            lines.append(f"• 💥 Худший сигнал: {worst:.1f}%")

    # Детали прогнозов
    if details:
        lines += ["", "*📋 Детали прогнозов:*"]
        for d in details[:6]:
            result_emoji = "✅" if d["result"] == "win" else "❌"
            pnl = d.get("pnl_pct") or 0
            direction = d.get("direction", "")
            asset = d.get("asset", "")
            date = (d.get("created_at") or "")[:10]
            news = (d.get("source_news") or "")[:50]
            lines.append(
                f"{result_emoji} *{asset}* {direction} → *{pnl:+.1f}%*\n"
                f"   _{date} | {news}..._"
            )

    # Фидбек пользователей
    lines += ["", f"{'─'*30}", "", "*💬 Фидбек пользователей:*"]
    if total_fb > 0:
        lines.append(f"• Оценок получено: {total_fb}")
        lines.append(f"• Считают полезным: {satisfaction:.0f}%")
    else:
        lines.append("• Оценок пока не получено")

    lines.append(f"• Активных пользователей: {users.get('active', 0)}")

    # Честный вывод
    lines += [
        "",
        f"{'─'*30}",
        "",
        "*🤝 Честно о результатах:*",
    ]

    if finished == 0:
        lines.append(
            "Прогнозы ещё проверяются. Track record формируется — "
            "через несколько недель будет полная картина."
        )
    elif winrate >= 55:
        lines.append(
            f"Хорошая неделя. Но {100-winrate:.0f}% прогнозов оказались неверными — "
            "рынок непредсказуем, это нормально. Используй как один из инструментов."
        )
    else:
        lines.append(
            "Слабая неделя — больше ошибок чем обычно. "
            "Скорее всего из-за высокой волатильности или противоречивых сигналов. "
            "Мы анализируем и улучшаем промпты агентов."
        )

    lines += [
        "",
        "⚠️ _Прошлые результаты не гарантируют будущих._",
        "_Не финансовый совет. DYOR._"
    ]

    return "\n".join(lines)


async def send_weekly_reports(bot, get_subscribers_fn):
    """Рассылает еженедельный отчёт всем подписчикам."""
    try:
        report = await build_weekly_report()
        subscribers = await get_subscribers_fn()

        sent = 0
        for user in subscribers:
            try:
                await bot.send_message(
                    user["user_id"],
                    report,
                    parse_mode="Markdown"
                )
                sent += 1
            except Exception as e:
                logger.warning(f"Weekly report send error for {user['user_id']}: {e}")

        logger.info(f"📊 Еженедельный отчёт отправлен {sent} пользователям")
        return sent

    except Exception as e:
        logger.error(f"Weekly report error: {e}")
        return 0
