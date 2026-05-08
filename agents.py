"""
agents.py — Система 4 AI-АГЕНТОВ-ДЕБАТЁРОВ v7.1

УЛУЧШЕНО v7.1 — АНТИГАЛЛЮЦИНАЦИОННАЯ СИСТЕМА:
1. СТАТИСТИЧЕСКИЙ ЗАПРЕТ: любая цифра без источника = автоудаление
2. Verifier: тег ❌ ГАЛЛЮЦИНАЦИЯ [УДАЛИТЬ] — Synth обязан игнорировать такие аргументы
3. Bull: запрет "7 из 10", "исторически", "по данным" без реального источника из контекста
4. Synth: явный запрет использовать аргументы помеченные Verifier как ГАЛЛЮЦИНАЦИЯ
5. Конкретные уровни входа/стопа/цели в торговом плане (обязательно)
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from ai_provider import ai
from config import DEBATE_ROUNDS, DISCLAIMER

logger = logging.getLogger(__name__)


@dataclass
class AgentMessage:
    agent: str
    content: str
    round_num: int


@dataclass
class DebateHistory:
    messages: list[AgentMessage] = field(default_factory=list)

    def add(self, agent: str, content: str, round_num: int):
        self.messages.append(AgentMessage(agent, content, round_num))

    def context_for_agent(self, max_chars: int = 4000) -> str:
        if not self.messages:
            return "Дебаты только начинаются."
        lines = []
        for m in self.messages:
            lines.append(f"[{m.agent} | Раунд {m.round_num}]:\n{m.content}")
        text = "\n\n".join(lines)
        if len(text) > max_chars:
            text = "...(сокращено)...\n\n" + text[-max_chars:]
        return text

    def last_message_by(self, agent_name: str) -> str:
        for m in reversed(self.messages):
            if agent_name in m.agent:
                return m.content
        return ""


COMMON_GROUNDING_RULE = """

🚨 АНТИГАЛЛЮЦИНАЦИОННЫЙ ПРОТОКОЛ — НАРУШЕНИЕ = ДИСКВАЛИФИКАЦИЯ:

ПРАВИЛО 1 — СТАТИСТИКА:
ЗАПРЕЩЕНО писать любые цифры/проценты/соотношения если их НЕТ в предоставленном контексте.
❌ ЗАПРЕЩЕНО: "7 из 10 случаев", "исторически 80%", "в 2020 году BTC вырос на 300%"
❌ ЗАПРЕЩЕНО: "по данным CoinDesk", "аналитики ожидают", "консенсус прогнозирует"
✅ РАЗРЕШЕНО: только цифры из блоков "РЕАЛЬНЫЕ РЫНОЧНЫЕ ДАННЫЕ" и "НОВОСТИ" в контексте

ПРАВИЛО 2 — ИСТОЧНИКИ:
Каждая цифра ОБЯЗАНА иметь тег: (Источник: Binance/Yahoo/FRED/Alpha Vantage/Finnhub)
Нет тега = Verifier ставит ❌ ГАЛЛЮЦИНАЦИЯ [УДАЛИТЬ]

ПРАВИЛО 3 — ИСТОРИЧЕСКИЕ АНАЛОГИИ:
❌ ЗАПРЕЩЕНО придумывать: "как в 2020 году", "аналогично 2022-му"
✅ РАЗРЕШЕНО: только если конкретная дата и цифра есть в переданном контексте

ПРАВИЛО 4 — FINBERT:
В контексте есть блок "FINBERT SENTIMENT". Используй ТОЛЬКО его значение.
Нельзя говорить "FinBERT подтверждает" если FinBERT MIXED или BEARISH.
"""

ANTI_HALLUCINATION_RULE = """

АБСОЛЮТНЫЙ ЗАПРЕТ НА ГАЛЛЮЦИНАЦИИ — НАРУШЕНИЕ = ДИСКВАЛИФИКАЦИЯ АРГУМЕНТА:

ЗАПРЕЩЕНО ПРИДУМЫВАТЬ (если нет в предоставленном контексте):
- "В X из Y случаев исторически..." -> ГАЛЛЮЦИНАЦИЯ, Verifier пометит удалением
- "Исторически BTC рос на X% после Fear < 15..." -> ГАЛЛЮЦИНАЦИЯ
- "В 2020/2022/2023 BTC вырос на X%..." -> ГАЛЛЮЦИНАЦИЯ если нет в данных
- "Аналитики прогнозируют X..." -> ГАЛЛЮЦИНАЦИЯ без источника из контекста
- Любые % роста/падения без прямой ссылки на источник из контекста -> ГАЛЛЮЦИНАЦИЯ
- Ставки конкретных банков (название банка + конкретный %) -> ГАЛЛЮЦИНАЦИЯ

ЕСЛИ ИСТОРИЧЕСКИХ ДАННЫХ НЕТ В КОНТЕКСТЕ:
-> Пиши честно: "Исторических данных в контексте нет - опираюсь только на текущие показатели"
-> НЕ ПРИДУМЫВАЙ статистику чтобы усилить аргумент

ЕДИНСТВЕННЫЕ РАЗРЕШЁННЫЕ ИСТОЧНИКИ:
- Блок "РЕАЛЬНЫЕ РЫНОЧНЫЕ ДАННЫЕ" - цены, изменения, индикаторы
- Блок "НОВОСТИ И ГЕОПОЛИТИКА" - конкретные события с датой
- Блок "FINBERT SENTIMENT" - sentiment score и confidence
- Веб-поиск если явно указан в контексте
- Всё остальное = ГАЛЛЮЦИНАЦИЯ = пометка Verifier на удаление
- Ставки конкретных банков (название банка + конкретный %) = ГАЛЛЮЦИНАЦИЯ
  Вместо этого пиши: "вклады до ~ключевая_ставка_цб% (уточняй у банка)"
"""



BULL_SYSTEM = """
Ты — Bull Researcher, БЫЧИЙ финансовый аналитик.

ТВОЯ ЗАДАЧА: найти бычьи аргументы ТОЛЬКО из предоставленных данных.

ФОРМАТ АРГУМЕНТА:
"• [Актив]: [ТОЧНАЯ цифра из контекста] → [почему бычий сигнал]
   Уверенность: ВЫСОКАЯ/СРЕДНЯЯ
   Источник: [FRED/Binance/Yahoo/Alpha Vantage/Finnhub]"

ОБЯЗАТЕЛЬНЫЕ БЛОКИ:

🔍 МОТИВЫ ИГРОКОВ (1-2 события):
"📌 [Событие из новостей]
  Кому выгодно: [кто конкретно]
  Кто теряет: [кто конкретно]
  Скрытый мотив: [что реально происходит]
  Рыночный вывод: [что конкретно покупать]"

⛓ ЭФФЕКТ 2-ГО ПОРЯДКА:
"📌 [Позитивное событие из данных]
→ 1й: [очевидный эффект]
→ 2й: [неочевидный эффект на смежном рынке]
→ 3й: [итог для портфеля]"

📊 FINBERT ОБЯЗАТЕЛЕН:
Найди в контексте "FINBERT SENTIMENT" и напиши ТОЧНОЕ значение:
- FinBERT BULLISH → "FinBERT подтверждает: [score] BULLISH [confidence]"
- FinBERT BEARISH → "FinBERT против. Объясняю почему данные важнее: [аргумент с цифрами из контекста]"
- FinBERT MIXED → "FinBERT нейтрален [score]. Данные из контекста говорят за рост: [конкретные цифры]"

🎯 СИГНАЛЫ ДЛЯ ДЕБАТОВ (ОБЯЗАТЕЛЬНО прочитай и используй):
В контексте есть блок "=== 🎯 СИГНАЛЫ ДЛЯ ДЕБАТОВ ===". 
Это твои бычьи аргументы — используй их как основу.
Если есть "🟢 БЫЧЬИ:" — цитируй эти сигналы с цифрами в аргументах.
Если есть "🔵 КРИТИЧЕСКИЙ СТОП-ФАКТОР: БЫЧИЙ" — используй как strongest argument.
Если есть "📊 СИСТЕМА БАЛЛОВ РЕКОМЕНДУЕТ: БЫЧИЙ" — это твой вывод.

🚨 АБСОЛЮТНЫЕ ЗАПРЕТЫ:
1. Золото/доллар/трежерис как бычий аргумент → немедленная дисквалификация
2. Любая статистика без источника из контекста → ❌ ГАЛЛЮЦИНАЦИЯ
3. "ARK Invest", "CoinDesk", "Seeking Alpha", "JPMorgan" — запрещены
4. "7 из 10 случаев", "исторически X%", "по данным аналитиков" — ЗАПРЕЩЕНО
5. "лучше подождать", "неопределённость" — ЗАПРЕЩЕНО

ПРАВИЛО КОРРЕЛЯЦИЙ:
RISK-ON (растут при оптимизме): BTC, ETH, акции, медь
RISK-OFF (растут при страхе): золото, доллар, трежерис

Максимум 4 аргумента. ОБЯЗАТЕЛЬНО заканчивай:
"Мой вывод: [актив] выглядит привлекательно потому что [X из данных контекста]."
""" + COMMON_GROUNDING_RULE


BULL_COUNTER_SYSTEM = """
Ты — Bull Researcher, отвечаешь на критику Bear и Verifier.

ОБЯЗАТЕЛЬНО:
1. Процитируй 2-3 аргумента Bear и опровергни каждый ЦИФРАМИ ИЗ КОНТЕКСТА
2. Если Verifier пометил твой аргумент ❌ ГАЛЛЮЦИНАЦИЯ — НЕ защищай его, признай и замени новым аргументом из данных
3. FinBERT: "FinBERT [точное значение из контекста] [подтверждает/не подтверждает] мою позицию"

ФОРМАТ:
"Bear говорит: '[цитата]'
Это неверно потому что: [контраргумент с источником из контекста]"

АБСОЛЮТНЫЙ ЗАПРЕТ:
- Золото/доллар как бычий аргумент
- Любая цифра без источника из контекста
- Защита аргументов помеченных Verifier как ❌ ГАЛЛЮЦИНАЦИЯ
""" + COMMON_GROUNDING_RULE


BEAR_SYSTEM = """
Ты — Bear Skeptic, скептичный риск-менеджер.

📊 FINBERT ОБЯЗАТЕЛЕН:
Найди "FINBERT SENTIMENT" в контексте и напиши ТОЧНОЕ значение:
- FinBERT BEARISH → "FinBERT подтверждает риски: [score] BEARISH [confidence]"
- FinBERT BULLISH → "FinBERT оптимистичен [score], но данные указывают на риски: [конкретные цифры]"
- FinBERT MIXED → "FinBERT неопределён [score] — в условиях неопределённости медвежий уклон безопаснее"

🎯 СИГНАЛЫ ДЛЯ ДЕБАТОВ (ОБЯЗАТЕЛЬНО прочитай и используй):
В контексте есть блок "=== 🎯 СИГНАЛЫ ДЛЯ ДЕБАТОВ ===".
Это твои медвежьи аргументы — используй их как основу.
Если есть "🔴 МЕДВЕЖИЙ:" — цитируй эти сигналы с цифрами в аргументах.
Если есть "🚨 КРИТИЧЕСКИЙ СТОП-ФАКТОР: МЕДВЕЖИЙ" — используй как strongest argument.
Если есть "📊 СИСТЕМА БАЛЛОВ РЕКОМЕНДУЕТ: МЕДВЕЖИЙ" — это твой вывод.
Если есть "⚠️ ВНИМАНИЕ:" — добавь к своим рискам.

ФОРМАТ РИСКА:
"• [Риск]: [конкретная цифра из контекста] → [почему опасно]
   Вероятность: ВЫСОКАЯ/СРЕДНЯЯ/НИЗКАЯ
   Источник: [из контекста]
   Хедж: [конкретная мера]"

⛓ ПРИЧИННО-СЛЕДСТВЕННЫЕ ЦЕПОЧКИ (только на основе данных контекста):
"[Триггер из данных] → [Реакция] → [Вторичные эффекты] → [Итог]"

🚨 ЗАПРЕТЫ:
- Любая статистика без источника из контекста → ❌ ГАЛЛЮЦИНАЦИЯ
- "ARK Invest", "CoinDesk", "Seeking Alpha" — запрещены
- "исторически X%", "по данным аналитиков" без реального источника — ЗАПРЕЩЕНО
- Максимум 5 рисков
- В первом раунде нет "Ответ на аргументы Bull"
""" + COMMON_GROUNDING_RULE


BEAR_COUNTER_SYSTEM = """
Ты - Bear Skeptic, углубляешь медвежью позицию.

ОБЯЗАТЕЛЬНО:
1. Процитируй Bull и опровергни ЦИФРАМИ из контекста
2. Используй ГАЛЛЮЦИНАЦИИ от Verifier против Bull - это твоё главное оружие
3. FinBERT: "FinBERT [score] [label] [confidence] подтверждает/опровергает Bull"

ТЕБЕ ТОЖЕ ЗАПРЕЩЕНО ГАЛЛЮЦИНИРОВАТЬ:
- НЕ пиши исторические примеры которых нет в контексте
- НЕ пиши "В марте 2020 BTC упал на X%" если нет в данных
- НЕ пиши "Аналитики Schwab/FT/Reuters говорят" если нет в данных
- Любая статистика только из предоставленного контекста

Используй только: цены, VIX, FinBERT, нефть, RSI из текущего контекста.

ЗАПРЕЩЕНО: "ARK Invest", "Schwab", нейтральный вывод
""" + COMMON_GROUNDING_RULE


VERIFIER_SYSTEM = """
Ты — Data Verifier. ГЛАВНЫЙ АНТИГАЛЛЮЦИНАЦИОННЫЙ АГЕНТ.

ТВОЯ ЗАДАЧА: найти и уничтожить все галлюцинации. Никаких рекомендаций.

---
ШАГ 1: ЦИФРЫ (сверяй с блоком "РЕАЛЬНЫЕ РЫНОЧНЫЕ ДАННЫЕ" в контексте)
Формат: "[показатель]: [значение агента] vs [значение в контексте] ✅/❌"

ШАГ 2: ОХОТА НА ГАЛЛЮЦИНАЦИИ 🎯
Для КАЖДОГО аргумента Bull и Bear проверяй:

а) Есть ли источник? Нет → ❌ ГАЛЛЮЦИНАЦИЯ [УДАЛИТЬ]
б) Есть ли цифра в контексте? Нет → ❌ ГАЛЛЮЦИНАЦИЯ [УДАЛИТЬ]
в) Историческая аналогия? Проверь есть ли она в контексте. Нет → ❌ ГАЛЛЮЦИНАЦИЯ [УДАЛИТЬ]
г) "7 из 10", "исторически X%", "аналитики ожидают" без источника → ❌ ГАЛЛЮЦИНАЦИЯ [УДАЛИТЬ]

Формат при обнаружении:
"❌ ГАЛЛЮЦИНАЦИЯ [УДАЛИТЬ]: '[цитата аргумента]'
   Причина: [нет источника / цифры нет в контексте / выдуманная статистика]
   Synth: этот аргумент ЗАПРЕЩЕНО использовать в вердикте"

ШАГ 3: ЛОГИКА
Bull:
- [аргумент]: ✅ ВЕРНО / ⚠️ УПРОЩЕНИЕ / ❌ ОШИБКА / ❌ ГАЛЛЮЦИНАЦИЯ [УДАЛИТЬ]
Bear:
- [аргумент]: ✅ ВЕРНО / ⚠️ УПРОЩЕНИЕ / ❌ ОШИБКА / ❌ ГАЛЛЮЦИНАЦИЯ [УДАЛИТЬ]

⚠️ ОСОБО ПРОВЕРЯЙ:
1. Золото/доллар как бычий аргумент → "❌ ЛОГИЧЕСКАЯ ОШИБКА: рост золото/доллар = Risk-off, НЕ бычий сигнал"
2. FinBERT игнорируется → "⚠️ FINBERT IGNORED: агент не использовал FinBERT из контекста"
3. Корреляции перепутаны → "❌ ОШИБКА КОРРЕЛЯЦИИ: [объяснение]"

ШАГ 4: ИТОГ ДЛЯ SYNTH
Список валидных аргументов (без галлюцинаций):
Bull ✅: [только подтверждённые аргументы]
Bear ✅: [только подтверждённые аргументы]
Галлюцинации удалены: [количество]
FinBERT из контекста: [score] [label] [confidence]

---
⛔ ЗАПРЕЩЕНО: рекомендации, выход за рамки 4 шагов
"""


SYNTH_SYSTEM = """
Ты — Consensus Synthesizer. Твоя задача: выдать структурированный JSON с вердиктом и планом.

📊 ОБЯЗАТЕЛЬНО УЧТИ СИСТЕМУ БАЛЛОВ:
- Если "СИСТЕМА БАЛЛОВ РЕКОМЕНДУЕТ: БЫЧИЙ" — склоняйся к бычьему
- Если "СИСТЕМА БАЛЛОВ РЕКОМЕНДУЕТ: МЕДВЕЖИЙ" — склоняйся к медвежьему
- Если "⚠️ СТОП-ФАКТОР" — следуй предупреждению

АЛГОРИТМ:
1. Проверь критические стоп-факторы (MVRV > 3.5 = ПРОДАВАТЬ, MVRV < 1.0 = ПОКУПАТЬ)
2. Посмотри систему баллов — какой вердикт рекомендуется
3. Проверь QE/QT режим (QT = -50% размера, QE = +50%)
4. Учти аргументы Bull и Bear
5. Прими финальное решение

ВЫВЕДИ ТОЛЬКО JSON (ничего другого!):

{
  "verdict": "МЕДВЕЖИЙ",
  "reason": "COT NET SHORT -4935 контрактов, SPY RSI 73.1 перекуплен",
  "plans": [
    {"symbol": "BTC", "direction": "SHORT", "entry": 79800, "stop": 82000, "target": 77000, "rr": "1:2", "size": "10%"},
    {"symbol": "SOL", "direction": "CASH", "trigger": "пробой $92"}
  ],
  "key_trigger": "пробой $82000 → подтверждение медвежьего тренда",
  "simple": "Фонды шортят BTC, SPY перекуплен — готовься к коррекции. COT NET SHORT -4935.",
  "qe_qt": "QT",
  "confidence": "HIGH"
}

ПРАВИЛА JSON:
- Только цифры из контекста (без источника = не пиши)
- R/R минимум 1:2
- entry/stop/target — числа (без $)
- plans: макс 3 позиции
- Если NEUTRAL: plans = [{"symbol": "CASH", "direction": "CASH", "trigger": "..."}]
- simple: 1-2 предложения ПРОСТЫМ ЯЗЫКОМ для непрофессионала
- confidence: HIGH / MEDIUM / LOW
""" + COMMON_GROUNDING_RULE

SPEECHWRITER_SYSTEM = """
Ты — Speechwriter. Тебе дают JSON от Synth:

{
  "verdict": "МЕДВЕЖИЙ",
  "reason": "...",
  "plans": [{"symbol": "BTC", "direction": "SHORT", "entry": 79800, "stop": 82000, "target": 77000, "rr": "1:2", "size": "10%"}, ...],
  "key_trigger": "...",
  "simple": "ПРОСТЫМИ СЛОВАМИ 1-2 предложения",
  "qe_qt": "QT",
  "confidence": "HIGH"
}

Твоя задача: превратить этот JSON в красивый, читаемый текст для Telegram.

ФОРМАТ ОТВЕТА:

🏆 ВЕРДИКТ СУДЬИ: [БЫЧИЙ/МЕДВЕЖИЙ/НЕЙТРАЛЬНЫЙ]
Потому что: [reason из JSON]

📋 ТОРГОВЫЙ ПЛАН:
• BTC | SHORT | Вход: $79800 | Стоп: $82000 | Цель: $77000 | R/R: 1:2 | 10% депозита
• SOL | CASH | Триггер: пробой $92
...

👀 КЛЮЧЕВОЙ ТРИГГЕР: [key_trigger из JSON]

💬 ПРОСТЫМИ СЛОВАМИ: [simple из JSON]

📊 QE/QT РЕЖИМ: [QE или QT или NEUTRAL] — ликвидность [растёт/падает/нейтральна]

⚡ КАК ЧИТАТЬ ПЛАН:
• Все цены — уровни для входа/стопа/цели
• R/R = соотношение риск/награда (1:2 = рискуем 1 чтобы заработать 2)
• % = доля депозита на эту сделку

НИЧЕГО КРОМЕ ФОРМАТИРОВАННОГО ТЕКСТА НЕ ВЫВОДИ.
"""



def _clean_agent_response(text: str) -> str:
    """
    Постобработка ответа агента.
    Cerebras 8B иногда повторяет системный промпт вместо ответа.
    Если текст содержит много 'ЗАПРЕЩЕНО' — это сигнал что модель
    повторяет промпт. Обрезаем до первого осмысленного абзаца.
    """
    if not text:
        return text
    # Считаем вхождения ЗАПРЕЩЕНО
    count_forbidden = text.count("ЗАПРЕЩЕНО")
    if count_forbidden >= 5:
        # Модель повторяет промпт — берём только первые 2 абзаца
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        # Ищем первый абзац без ЗАПРЕЩЕНО
        useful = []
        for p in paragraphs:
            if "ЗАПРЕЩЕНО" not in p and len(p) > 50:
                useful.append(p)
            if len(useful) >= 3:
                break
        if useful:
            return "\n\n".join(useful)
    return text


# ─── БАЗОВЫЙ АГЕНТ ────────────────────────────────────────────────────────────

class BaseAgent:
    def __init__(self, name: str, emoji: str, system_prompt: str, ai_method: str):
        self.name          = name
        self.emoji         = emoji
        self.system_prompt = system_prompt
        self.ai_method     = ai_method

    async def respond(
        self,
        news_context: str,
        debate_history: DebateHistory,
        round_num: int,
        extra_instruction: str = ""
    ) -> str:
        history_ctx = debate_history.context_for_agent()
        prompt = f"""КОНТЕКСТ И ДАННЫЕ (ИСПОЛЬЗУЙ ТОЛЬКО ЭТИ ДАННЫЕ):
{news_context}

ИСТОРИЯ ДЕБАТОВ:
{history_ctx}

{f'ДОПОЛНИТЕЛЬНАЯ ИНСТРУКЦИЯ:{chr(10)}{extra_instruction}' if extra_instruction else ''}

Сейчас РАУНД {round_num} из {DEBATE_ROUNDS}.

🚨 НАПОМИНАНИЕ АНТИГАЛЛЮЦИНАЦИОННОГО ПРОТОКОЛА:
- Любая цифра ТОЛЬКО из блока "РЕАЛЬНЫЕ РЫНОЧНЫЕ ДАННЫЕ" выше
- FinBERT: используй ТОЧНОЕ значение из блока "FINBERT SENTIMENT"
- Нет источника = не пиши эту цифру"""

        try:
            caller   = getattr(ai, self.ai_method)
            response = await caller(prompt=prompt, system=self.system_prompt)
            return response
        except Exception as e:
            logger.error(f"Agent {self.name} error: {e}")
            return f"[Ошибка агента {self.name}: {e}]"


# ─── КОНКРЕТНЫЕ АГЕНТЫ ────────────────────────────────────────────────────────

class BullResearcher(BaseAgent):
    def __init__(self):
        super().__init__("Bull Researcher", "🐂", BULL_SYSTEM, "bull")

    async def respond_counter(self, news_context: str, history: DebateHistory, round_num: int) -> str:
        bear_args          = history.last_message_by("Bear")
        verifier_notes     = history.last_message_by("Verifier")
        extra              = ""
        if bear_args:
            extra += f"Аргументы Bear:\n{bear_args[:1000]}\n\n"
        if verifier_notes:
            extra += f"⚠️ Verifier нашёл галлюцинации — НЕ защищай их:\n{verifier_notes[:800]}"
        self.system_prompt = BULL_COUNTER_SYSTEM
        result             = await self.respond(news_context, history, round_num, extra)
        self.system_prompt = BULL_SYSTEM
        return result


class BearSkeptic(BaseAgent):
    def __init__(self):
        super().__init__("Bear Skeptic", "🐻", BEAR_SYSTEM, "bear")

    async def respond_counter(self, news_context: str, history: DebateHistory, round_num: int) -> str:
        bull_counter       = history.last_message_by("Bull")
        verifier_notes     = history.last_message_by("Verifier")
        extra = ""
        if bull_counter:
            extra += f"Ответ Bull:\n{bull_counter[:1000]}\n\n"
        if verifier_notes:
            import re as _re
            hall = _re.findall(r"ГАЛЛЮЦИНАЦИЯ[^\n]*", verifier_notes)
            if hall:
                extra += "Галлюцинации Bull (Verifier, используй):\n" + "\n".join(hall[:5]) + "\n\n"
            extra += f"Verifier:\n{verifier_notes[:500]}"
        self.system_prompt = BEAR_COUNTER_SYSTEM
        result             = await self.respond(news_context, history, round_num, extra)
        self.system_prompt = BEAR_SYSTEM
        # Постобработка: если модель повторяет системный промпт — обрезаем
        result = _clean_agent_response(result)
        return result


class DataVerifier(BaseAgent):
    def __init__(self):
        super().__init__("Data Verifier", "🔍", VERIFIER_SYSTEM, "verifier")


class ConsensusSynth(BaseAgent):
    def __init__(self):
        super().__init__("Consensus Synthesizer", "⚖️", SYNTH_SYSTEM, "synth")


_SPEECHWRITER_TIMEOUT_S = 45.0
_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z0-9]*\s*\n?|\n?```\s*$", re.MULTILINE)


def _strip_code_fences(text: str) -> str:
    """Some LLMs wrap output in ```...``` even when told not to. Strip them."""
    if not text:
        return text
    cleaned = _CODE_FENCE_RE.sub("", text).strip()
    return cleaned or text


def _extract_json_obj(text: str) -> dict | None:
    """Extract the first JSON object from arbitrary LLM text. Returns None on failure."""
    if not text:
        return None
    raw = _strip_code_fences(text).strip()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _format_price(value) -> str:
    """Format a price-like value for the trade plan line."""
    if value is None or value == "":
        return "—"
    if isinstance(value, (int, float)):
        if value >= 1000:
            return f"${value:,.0f}".replace(",", " ")
        return f"${value:g}"
    s = str(value).strip()
    if s and s[0].isdigit():
        return f"${s}"
    return s


def _render_trade_plan_from_json(data: dict) -> str:
    """Deterministic Telegram-ready text rendering of Synth JSON.
    Used when Synth returns parseable JSON, avoiding an extra LLM call entirely.
    """
    verdict = str(data.get("verdict", "НЕЙТРАЛЬНЫЙ")).upper().strip() or "НЕЙТРАЛЬНЫЙ"
    reason = str(data.get("reason", "")).strip()
    plans = data.get("plans") or []
    key_trigger = str(data.get("key_trigger", "")).strip()
    simple = str(data.get("simple", "")).strip()
    qe_qt = str(data.get("qe_qt", "NEUTRAL")).upper().strip() or "NEUTRAL"

    lines: list[str] = []
    lines.append(f"🏆 ВЕРДИКТ СУДЬИ: {verdict}")
    if reason:
        lines.append(f"Потому что: {reason}")
    lines.append("")
    lines.append("📋 ТОРГОВЫЙ ПЛАН:")
    if isinstance(plans, list) and plans:
        for p in plans:
            if not isinstance(p, dict):
                continue
            sym = str(p.get("symbol", "")).upper() or "?"
            direction = str(p.get("direction", "")).upper()
            if direction in {"CASH", "WAIT", "FLAT"}:
                trigger = str(p.get("trigger") or p.get("entry") or "—")
                lines.append(f"• {sym} | CASH | Триггер: {trigger}")
                continue
            entry = _format_price(p.get("entry"))
            stop = _format_price(p.get("stop"))
            target = _format_price(p.get("target"))
            rr = str(p.get("rr", "")).strip() or "—"
            size = str(p.get("size", "")).strip() or "—"
            lines.append(
                f"• {sym} | {direction or '—'} | Вход: {entry} | Стоп: {stop} | Цель: {target} | R/R: {rr} | {size} депозита"
            )
    else:
        lines.append("• нет идей с положительным ожиданием — стой в стороне")
    lines.append("")
    if key_trigger:
        lines.append(f"👀 КЛЮЧЕВОЙ ТРИГГЕР: {key_trigger}")
        lines.append("")
    if simple:
        lines.append(f"💬 ПРОСТЫМИ СЛОВАМИ: {simple}")
        lines.append("")
    qe_qt_word = {"QE": "растёт", "QT": "падает"}.get(qe_qt, "нейтральна")
    lines.append(f"📊 QE/QT РЕЖИМ: {qe_qt} — ликвидность {qe_qt_word}")
    return "\n".join(lines).rstrip()


class Speechwriter:
    """Speechwriter — форматирует JSON от Synth в красивый текст для Telegram."""

    def __init__(self):
        self.system_prompt = SPEECHWRITER_SYSTEM

    async def format(self, synth_json: str) -> str:
        """
        Принимает JSON (или текст) от Synth и превращает в читаемый торговый план.

        Стратегия:
          1. Если Synth вернул валидный JSON → рендерим детерминированно (быстро,
             без LLM-вызова, без галлюцинаций, цифры один-в-один).
          2. Иначе → зовём LLM с таймаутом и пост-обработкой (снятие ```code fences```).
          3. Если и это сломалось → возвращаем сырой ввод как fallback.
        """
        # 1. Strict path: deterministic rendering when Synth returned clean JSON.
        data = _extract_json_obj(synth_json)
        if data is not None:
            try:
                rendered = _render_trade_plan_from_json(data)
                if rendered.strip():
                    logger.info("[SPEECHWRITER] Deterministic render OK (no LLM call)")
                    return rendered
            except Exception as e:
                logger.warning(f"[SPEECHWRITER] Deterministic render failed, falling back to LLM: {e}")

        # 2. LLM fallback for free-form Synth output.
        from ai_provider import ai

        prompt = f"""Преобразуй данные ниже в читаемый торговый план:

{synth_json}

ФОРМАТ:
🏆 ВЕРДИКТ СУДЬИ: [БЫЧИЙ/МЕДВЕЖИЙ/НЕЙТРАЛЬНЫЙ]
Потому что: [1 предложение]

📋 ТОРГОВЫЙ ПЛАН:
• BTC | SHORT | Вход: $79800 | Стоп: $82000 | Цель: $77000 | R/R: 1:2 | 10% депозита

👀 КЛЮЧЕВОЙ ТРИГГЕР: [цена или событие]

💬 ПРОСТЫМИ СЛОВАМИ: [1-2 предложения для непрофессионала]

📊 QE/QT РЕЖИМ: [QE/QT/NEUTRAL]

НИЧЕГО КРОМЕ ФОРМАТИРОВАННОГО ТЕКСТА НЕ ВЫВОДИ.
"""

        try:
            response = await asyncio.wait_for(
                ai.synth(prompt=prompt, system=self.system_prompt),
                timeout=_SPEECHWRITER_TIMEOUT_S,
            )
            return _strip_code_fences(response or "") or synth_json
        except asyncio.TimeoutError:
            logger.warning(f"[SPEECHWRITER] timed out after {_SPEECHWRITER_TIMEOUT_S}s, returning raw synth")
            return synth_json
        except Exception as e:
            logger.error(f"Speechwriter error: {e}")
            return synth_json  # fallback — выводим как есть


# ─── ОРКЕСТРАТОР ──────────────────────────────────────────────────────────────

class DebateOrchestrator:
    def __init__(self):
        self.bull      = BullResearcher()
        self.bear      = BearSkeptic()
        self.verifier  = DataVerifier()
        self.synth     = ConsensusSynth()
        self.writer    = Speechwriter()

    async def run_debate(
        self,
        news_context: str,
        market_data: str = "",
        custom_mode: bool = False,
        live_prices: str = "",
        profile_instruction: str = ""
    ) -> str:
        history = DebateHistory()
        rounds  = DEBATE_ROUNDS if not custom_mode else min(DEBATE_ROUNDS, 3)
        logger.info(f"Запускаю дебаты v7.1: {rounds} раундов")

        full_context = ""
        if live_prices:
            full_context += "=== РЕАЛЬНЫЕ РЫНОЧНЫЕ ДАННЫЕ ===\n" + live_prices + "\n\n"
        full_context += "=== НОВОСТИ И ГЕОПОЛИТИКА ===\n" + news_context
        if market_data:
            full_context += "\n\n=== ДОП. ДАННЫЕ ===\n" + market_data
        if profile_instruction:
            full_context += "\n\n=== ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ ===\n" + profile_instruction

        # Раунд 1 — Bull и Bear независимо
        logger.info("Раунд 1: Bull и Bear независимо...")
        empty_history    = DebateHistory()
        bull_r1, bear_r1 = await asyncio.gather(
            self.bull.respond(full_context, empty_history, round_num=1),
            self.bear.respond(full_context, empty_history, round_num=1)
        )
        history.add(f"{self.bull.emoji} {self.bull.name}", bull_r1, 1)
        history.add(f"{self.bear.emoji} {self.bear.name}", bear_r1, 1)

        # Раунд 2 — Verifier проверяет галлюцинации, Bull отвечает
        if rounds >= 2:
            logger.info("Раунд 2: Verifier охотится на галлюцинации...")
            verify_r2 = await self.verifier.respond(full_context, history, round_num=2)
            history.add(f"{self.verifier.emoji} {self.verifier.name}", verify_r2, 2)
            bull_r2 = await self.bull.respond_counter(full_context, history, round_num=2)
            history.add(f"{self.bull.emoji} {self.bull.name}", bull_r2, 2)

        # Раунд 3 — Bear добивает галлюцинации Bull
        if rounds >= 3:
            logger.info("Раунд 3: Bear добивает галлюцинации...")
            bear_r3 = await self.bear.respond_counter(full_context, history, round_num=3)
            history.add(f"{self.bear.emoji} {self.bear.name}", bear_r3, 3)

        # Доп раунды
        for extra_round in range(4, rounds + 1):
            bull_x = await self.bull.respond_counter(full_context, history, extra_round)
            history.add(f"{self.bull.emoji} {self.bull.name}", bull_x, extra_round)
            bear_x = await self.bear.respond_counter(full_context, history, extra_round)
            history.add(f"{self.bear.emoji} {self.bear.name}", bear_x, extra_round)

        logger.info("Финальный синтез: Synth (JSON) → Speechwriter (формат)...")
        
        # Шаг 1: Synth → компактный JSON
        synth_json = await self.synth.respond(full_context, history, round_num=rounds)
        logger.info(f"[SPEECHWRITER] Raw synth output: {synth_json[:300]}")
        
        # Шаг 2: Speechwriter → красивый форматированный текст
        try:
            final_synthesis = await self.writer.format(synth_json)
        except Exception as e:
            logger.warning(f"[SPEECHWRITER] Error, using raw synth: {e}")
            final_synthesis = synth_json

        # ─── Hallucination tracking ───────────────────────────────────────────
        try:
            from ai_provider import track_hallucinations, log_hallucination_stats

            verifier_text = (history.last_message_by("Verifier") or "")[:2000]
            bull_all = " ".join(m.content for m in history.messages if "Bull" in m.agent)
            bear_all = " ".join(m.content for m in history.messages if "Bear" in m.agent)

            # Count total agent arguments (paragraphs that look like arguments)
            bull_args = max(1, len([p for p in bull_all.split("\n") if p.strip().startswith("•") or p.strip().startswith("-")]))
            bear_args = max(1, len([p for p in bear_all.split("\n") if p.strip().startswith("•") or p.strip().startswith("-")]))

            # Verifier marks hallucinations per-line; attribute each one to Bull or Bear
            # by looking for the agent name in the same line. Lines mentioning neither
            # are ignored (avoids the previous bug where ALL hallucinations were attributed
            # to Bull, and Bear count was a divided-by-2 hack).
            bull_halls = 0
            bear_halls = 0
            for line in verifier_text.splitlines():
                if "ГАЛЛЮЦИНАЦИЯ" not in line:
                    continue
                if "Bull" in line:
                    bull_halls += 1
                if "Bear" in line:
                    bear_halls += 1

            track_hallucinations("bull", bull_args, bull_halls)
            track_hallucinations("bear", bear_args, bear_halls)
            log_hallucination_stats()
            logger.info(f"[HALLUCINATION] Bull: {bull_halls}/{bull_args}, Bear: {bear_halls}/{bear_args}")
        except Exception as _e:
            logger.debug(f"[HALLUCINATION] Tracking skipped: {_e}")

        return self._format_report(history, final_synthesis, news_context, custom_mode)

    def _format_report(self, history, synthesis, news_context, custom_mode) -> str:
        now   = datetime.now().strftime("%d.%m.%Y %H:%M")
        title = "🔍 *АНАЛИЗ НОВОСТИ*" if custom_mode else "📊 *DIALECTIC EDGE — DAILY*"

        try:
            from ai_provider import get_models_summary
            models_line = get_models_summary()
        except Exception:
            models_line = "🐂 Bull | 🐻 Bear | 🔍 Verifier | ⚖️ Synth → ✍️ Speechwriter"

        honest_header = (
            "💬 *Прежде чем читать:*\n"
            "Это структурированный AI-анализ на реальных данных.\n"
            f"{models_line}\n"
        )

        report_parts = [title, f"🕐 _{now}_", "", honest_header, "─" * 30, ""]
        report_parts.append("🗣 *ХОД ДЕБАТОВ*\n")

        curr_r = 0
        for m in history.messages:
            if m.round_num != curr_r:
                curr_r = m.round_num
                report_parts.append(f"\n*── Раунд {curr_r} ──*\n")
            report_parts.append(f"{m.agent}:\n{m.content}\n")

        report_parts.append("─" * 30)
        report_parts.append("⚖️ *ВЕРДИКТ И ТОРГОВЫЙ ПЛАН*\n")
        report_parts.append(synthesis)
        report_parts.append(DISCLAIMER)

        report = "\n".join(str(p) for p in report_parts)
        # Убираем иероглифы которые иногда добавляет Groq/Llama
        import re as _re
        report = _re.sub(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+', '', report)
        report = _re.sub(r'  +', ' ', report)
        return report
