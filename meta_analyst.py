"""
meta_analyst.py — Сравнение с западными аналитиками.

Собирает публичные прогнозы крупных домов:
Goldman Sachs, JPMorgan, Morgan Stanley, Bernstein, ARK Invest и др.

Агенты сравнивают свой анализ с "соседом":
- Совпадает → усиливает сигнал
- Расходится → объясняет ПОЧЕМУ и кто исторически был прав
"""

import asyncio
import logging
import aiohttp
import feedparser
from datetime import datetime

logger = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=12)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DialecticEdge/4.0)"}


# ─── Публичные источники прогнозов крупных домов ──────────────────────────────

ANALYST_SOURCES = [
    # Goldman Sachs — публичные комментарии
    {
        "name": "Goldman Sachs",
        "emoji": "🏦",
        "rss": "https://www.goldmansachs.com/insights/rss.xml",
        "keywords": ["outlook", "forecast", "target", "expect", "price target"]
    },
    # JPMorgan
    {
        "name": "JPMorgan",
        "emoji": "🏛️",
        "rss": "https://www.jpmorgan.com/feeds/rss/insights",
        "keywords": ["forecast", "outlook", "target", "bullish", "bearish"]
    },
    # ARK Invest — очень публичны, активно публикуют прогнозы
    {
        "name": "ARK Invest",
        "emoji": "🚀",
        "rss": "https://ark-invest.com/feed/",
        "keywords": ["bitcoin", "crypto", "forecast", "target", "innovation"]
    },
    # Bloomberg Opinion — публичные колонки аналитиков
    {
        "name": "Bloomberg Opinion",
        "emoji": "📰",
        "rss": "https://feeds.bloomberg.com/bloomberg/opinioncolumns.rss",
        "keywords": ["market", "stocks", "bitcoin", "fed", "inflation", "forecast"]
    },
    # Reuters Markets
    {
        "name": "Reuters Markets",
        "emoji": "📡",
        "rss": "https://feeds.reuters.com/reuters/businessNews",
        "keywords": ["forecast", "target", "analyst", "rating", "outlook", "predict"]
    },
    # The Street — retail analyst opinions
    {
        "name": "TheStreet",
        "emoji": "📊",
        "rss": "https://www.thestreet.com/rss/feeds/latest-news.xml",
        "keywords": ["buy", "sell", "target", "forecast", "analyst"]
    },
    # Seeking Alpha — aggregates many analyst views
    {
        "name": "Seeking Alpha",
        "emoji": "🔎",
        "rss": "https://seekingalpha.com/feed.xml",
        "keywords": ["forecast", "target price", "buy", "sell", "bullish", "bearish"]
    },
    # CoinDesk — crypto specific analysts
    {
        "name": "CoinDesk Analysis",
        "emoji": "₿",
        "rss": "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "keywords": ["price target", "forecast", "analyst", "prediction", "outlook"]
    },
]


async def fetch_analyst_views() -> list[dict]:
    """
    Собирает свежие прогнозы от крупных аналитических домов.
    Возвращает список {source, title, summary, url, date}.
    """
    all_views = []

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        for source in ANALYST_SOURCES:
            try:
                async with session.get(
                    source["rss"],
                    timeout=TIMEOUT
                ) as resp:
                    if resp.status != 200:
                        continue
                    content = await resp.text()

                feed = feedparser.parse(content)

                for entry in feed.entries[:15]:
                    title = entry.get("title", "").lower()
                    summary = entry.get("summary", "") or entry.get("description", "")

                    # Фильтруем только аналитические материалы
                    text = (title + " " + summary.lower())
                    if any(kw in text for kw in source["keywords"]):
                        all_views.append({
                            "source": source["name"],
                            "emoji": source["emoji"],
                            "title": entry.get("title", "")[:150],
                            "summary": summary[:300],
                            "url": entry.get("link", ""),
                            "date": entry.get("published", "")[:16]
                        })

                    if len([v for v in all_views
                            if v["source"] == source["name"]]) >= 3:
                        break

                await asyncio.sleep(0.3)

            except Exception as e:
                logger.debug(f"{source['name']} RSS error: {e}")
                continue

    return all_views


def format_analyst_views(views: list[dict]) -> str:
    """Форматирует прогнозы аналитиков для агентов."""
    if not views:
        return ""

    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    lines = [
        f"=== ПРОГНОЗЫ ЗАПАДНЫХ АНАЛИТИКОВ ({now}) ===",
        "Используй для сравнения. Если твой вывод расходится — объясни почему.\n"
    ]

    by_source = {}
    for v in views:
        src = v["source"]
        if src not in by_source:
            by_source[src] = []
        by_source[src].append(v)

    for source, items in by_source.items():
        emoji = items[0]["emoji"]
        lines.append(f"[{emoji} {source}]")
        for item in items[:2]:
            lines.append(f"  • {item['title']}")
            if item["summary"] and len(item["summary"]) > 50:
                clean = item["summary"].replace("<p>", "").replace("</p>", "")
                lines.append(f"    {clean[:200]}")
        lines.append("")

    lines.append(
        "ИНСТРУКЦИЯ ДЛЯ АГЕНТОВ:\n"
        "Сравни свои выводы с прогнозами выше.\n"
        "Если совпадаете → это усиливает сигнал, укажи это явно.\n"
        "Если расходитесь → объясни ПОЧЕМУ ты видишь иначе.\n"
        "Не копируй мнение аналитиков слепо — думай самостоятельно."
    )

    return "\n".join(lines)


# ─── Промпт для агента-сравнителя ─────────────────────────────────────────────

META_COMPARISON_PROMPT = """
=== БЛОК СРАВНЕНИЯ С ЗАПАДНЫМИ АНАЛИТИКАМИ ===

После завершения основного синтеза добавь раздел:

🌐 *СРАВНЕНИЕ С РЫНКОМ*

Для каждого ключевого вывода:
1. Совпадает ли с консенсусом крупных домов?
   - ✅ КОНСЕНСУС СОВПАДАЕТ: [кто именно, что говорят]
     → Это усиливает сигнал. Большинство аналитиков видят то же.
   - ⚠️ РАСХОЖДЕНИЕ С КОНСЕНСУСОМ: [кто именно, что говорят они]
     → Мы видим иначе потому что: [конкретная причина]
     → Кто может быть прав: [честная оценка]
   - ❓ НЕТ ДАННЫХ О КОНСЕНСУСЕ: прогнозы по этому активу не найдены

2. Исторический счёт:
   Если есть данные — кто чаще был прав в похожих ситуациях?

3. Контрарианский взгляд:
   Если все аналитики единодушны → это само по себе риск.
   Рынок уже учёл консенсус в цене?

Важно: не льсти крупным домам. Goldman Sachs ошибается так же как все.
В 2022 большинство банков не предсказали крах FTX и не видели инфляционный
шок заранее. Твой независимый анализ данных может быть точнее их нарратива.
"""


async def get_meta_context() -> str:
    """
    Главная функция — собирает всё для блока сравнения.
    """
    logger.info("🌐 Собираю прогнозы западных аналитиков...")
    views = await fetch_analyst_views()

    if not views:
        logger.warning("Прогнозы аналитиков не получены")
        return ""

    formatted = format_analyst_views(views)
    logger.info(f"✅ Получено {len(views)} прогнозов от {len(set(v['source'] for v in views))} источников")

    return formatted + "\n\n" + META_COMPARISON_PROMPT
