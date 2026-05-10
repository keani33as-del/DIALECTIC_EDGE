"""
Helpers for extracting a structured digest summary from full model reports.
"""

from __future__ import annotations

import re
from typing import Optional

VERDICT_LABELS = {
    "BUY": "Бычий",
    "SELL": "Медвежий",
    "NEUTRAL": "Нейтральный",
}

VERDICT_EMOJIS = {
    "BUY": "🟢",
    "SELL": "🔴",
    "NEUTRAL": "⚪️",
}

_SECTION_BREAK_RE = re.compile(r"^(?:[-_=]{3,}|[╔═]{3,}|[⚖🏆📊📋💬👀🚨⚡])")
_SYMBOL_RE = re.compile(r"\b([A-Z]{2,10}(?:=F)?)\b")


def _strip_markup(text: str) -> str:
    text = text or ""
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\*(.*?)\*", r"\1", text)
    text = re.sub(r"_(.*?)_", r"\1", text)
    text = re.sub(r"`(.*?)`", r"\1", text)
    return text.strip()


def _clean_line(text: str) -> str:
    text = _strip_markup(text)
    text = re.sub(r"\s+", " ", text).strip(" •-—–\t")
    return text


def _parse_price(raw: str) -> Optional[float]:
    raw = (raw or "").strip()
    if not raw:
        return None
    raw = raw.replace("$", "").replace(",", "").replace(" ", "")
    raw = raw.replace("к", "K").replace("К", "K")
    if raw.upper().endswith("K"):
        try:
            return float(raw[:-1]) * 1000
        except ValueError:
            return None
    try:
        return float(raw)
    except ValueError:
        return None


def _format_price(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if abs(value - round(value)) < 0.01:
        return f"${value:,.0f}"
    if abs(value) >= 100:
        return f"${value:,.2f}"
    return f"${value:,.4f}"


def _unique_lines(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = _clean_line(line)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def normalize_verdict(value: str | None) -> str:
    text = (value or "").upper()
    if any(token in text for token in ("БЫЧ", "BULL", "LONG", "BUY")):
        return "BUY"
    if any(token in text for token in ("МЕДВ", "BEAR", "SHORT", "SELL")):
        return "SELL"
    return "NEUTRAL"


def extract_verdict(report_text: str) -> str:
    clean = _strip_markup(report_text)

    for pattern in (
        r"ВЕРДИКТ СУДЬИ\s*[:：]\s*([^\n]+)",
        r"ВЕРДИКТ\s*[:：]\s*([^\n]+)",
    ):
        match = re.search(pattern, clean, re.IGNORECASE)
        if match:
            return normalize_verdict(match.group(1))

    synth_markers = (
        "ВЕРДИКТ И ТОРГОВЫЙ ПЛАН",
        "ИТОГОВЫЙ СИНТЕЗ",
    )
    upper = clean.upper()
    for marker in synth_markers:
        idx = upper.find(marker)
        if idx != -1:
            return normalize_verdict(clean[idx : idx + 1200])

    return "NEUTRAL"


def _extract_single_line(report_text: str, label: str) -> str:
    clean = _strip_markup(report_text)
    pattern = rf"{label}\s*[:：]\s*([^\n]+)"
    match = re.search(pattern, clean, re.IGNORECASE)
    return _clean_line(match.group(1)) if match else ""


_LEADING_PUNCT_RE = re.compile(r"^[\s:;,.—–\-]+")


def _strip_leading_punct(text: str) -> str:
    """«: SPY перекуплен» → «SPY перекуплен». Synth-модель иногда отдаёт значение
    `simple` с ведущим двоеточием (паротирует формат `simple: ...`),
    и в дайджесте это выглядит как `Простыми словами: : SPY ...`."""
    return _LEADING_PUNCT_RE.sub("", text or "").strip()


# ───── plan-actionability helpers (зеркало agents._is_*_trigger) ─────
# Дублируем здесь, а не импортируем из agents.py: digest_context — leaf-модуль
# под core/, agents.py тащит за собой LLM-провайдеров и тяжёлый рантайм. Делать
# core/ зависимым от agents.py — гарантированный круговой импорт в будущем.

_BIDIRECTIONAL_TRIGGER_RE = re.compile(
    r"(?:вниз|внизу|ниже|вверх|выше|сверху).*(?:или|либо|или\s+/|\s+/\s+).*"
    r"(?:вверх|выше|сверху|вниз|внизу|ниже)",
    re.IGNORECASE | re.DOTALL,
)
_PRICE_LEVEL_RE = re.compile(r"\$?\s*\d[\d.,]*\s*[KkКк]?")


def _is_bidirectional_trigger(text: str) -> bool:
    if not text:
        return False
    s = str(text).lower()
    if _BIDIRECTIONAL_TRIGGER_RE.search(s):
        return True
    has_up = any(tok in s for tok in ("вверх", "выше", "сверху", "above", "up"))
    has_down = any(tok in s for tok in ("вниз", "ниже", "снизу", "below", "down"))
    has_or = any(tok in s for tok in (" или ", " либо ", " / ", " or "))
    return has_up and has_down and has_or


def _is_vague_trigger(text: str) -> bool:
    if not text:
        return True
    s = str(text).strip()
    if len(s) < 6:
        return True
    has_level = bool(_PRICE_LEVEL_RE.search(s))
    has_concrete_event = any(
        tok in s.lower()
        for tok in (
            "пробой", "закрытие", "тест", "ретест", "касание",
            "rsi", "atr", "vix", "ema", "sma", "macd",
            "fomc", "cpi", "nfp", "ставк", "fed", "ecb",
            "breakout", "break", "close above", "close below",
        )
    )
    return not (has_level or has_concrete_event)


def _is_unactionable_cash_plan(plan: dict) -> tuple[bool, str]:
    """CASH-план без однозначного направления (двунаправленный или абстрактный
    триггер) → демоут в watch. Также демоутим LONG/SHORT-планы у которых нет
    ни одного из (entry, stop, target) — это значит парсер поймал направление
    из текста triggera, но реальной сделки не сформулировано.
    Возвращает (is_unactionable, reason)."""
    direction = (plan.get("direction") or "").upper().strip()
    label_upper = str(plan.get("label") or "").upper()
    
    # Defense-in-depth: WATCH или label-CASH/WATCH/ВНЕ РЫНКА → всегда watch.
    if direction in {"WATCH", "WAIT", "FLAT"}:
        return True, "watch direction"
    if any(x in label_upper for x in ("CASH", "WATCH", "ВНЕ РЫНКА", "НАБЛЮД")):
        # Явно cash/watch — даже если direction неправильно тегнут как LONG.
        if direction in {"LONG", "SHORT"}:
            entry = plan.get("entry")
            stop = plan.get("stop")
            target = plan.get("target")
            if not entry and not stop and not target:
                return True, "label says CASH but direction got tagged from trigger text"
    
    # LONG/SHORT-план без вход/стоп/цели — мусор от парсера (или AI не дал
    # цифр). Не показываем юзеру «BTC LONG — вход —, стоп —, цель —».
    if direction in {"LONG", "SHORT"}:
        entry = plan.get("entry")
        stop = plan.get("stop")
        target = plan.get("target")
        if not entry and not stop and not target:
            return True, "actionable plan without entry/stop/target"
    
    if direction not in {"CASH", "WAIT", "FLAT"}:
        return False, ""
    trigger = str(plan.get("trigger") or "").strip()
    if not trigger or trigger == "—":
        return True, "no trigger"
    if _is_bidirectional_trigger(trigger):
        return True, "bidirectional"
    if _is_vague_trigger(trigger):
        return True, "vague"
    return False, ""


def extract_plain_language(report_text: str) -> str:
    clean = _strip_markup(report_text)
    markers = (
        "ПРОСТЫМИ СЛОВАМИ",
        "💬 ПРОСТЫМИ СЛОВАМИ",
    )

    for marker in markers:
        idx = clean.upper().find(marker)
        if idx == -1:
            continue
        block = clean[idx + len(marker) : idx + len(marker) + 900]
        lines: list[str] = []
        for raw_line in block.splitlines():
            line = _clean_line(raw_line)
            if not line:
                if lines:
                    break
                continue
            if _SECTION_BREAK_RE.match(line) and lines:
                break
            if len(line) < 8:
                continue
            lines.append(line)
            if len(" ".join(lines)) >= 320:
                break
        if lines:
            return _strip_leading_punct(" ".join(lines))[:360]
    return ""


def extract_eli5(report_text: str) -> str:
    """«👶 КАК 5-ЛЕТНЕМУ: …» — отдельное поле от Synth, дополняющее «простыми
    словами». Юзер просил оставить «простыми словами» как есть и **дополнить**
    супер-простым объяснением.
    """
    clean = _strip_markup(report_text)
    markers = (
        "КАК 5-ЛЕТНЕМУ",
        "👶 КАК 5-ЛЕТНЕМУ",
        "5-ЛЕТНЕМУ",
        "ELI5",
    )
    upper = clean.upper()
    for marker in markers:
        idx = upper.find(marker)
        if idx == -1:
            continue
        block = clean[idx + len(marker) : idx + len(marker) + 600]
        lines: list[str] = []
        for raw_line in block.splitlines():
            line = _clean_line(raw_line)
            if not line:
                if lines:
                    break
                continue
            if _SECTION_BREAK_RE.match(line) and lines:
                break
            if len(line) < 6:
                continue
            lines.append(line)
            if len(" ".join(lines)) >= 280:
                break
        if lines:
            return _strip_leading_punct(" ".join(lines))[:320]
    return ""


_WATCH_HEADERS = (
    "СЕЙЧАС НЕ ТОРГУЕМ — СЛЕДИМ ЗА УРОВНЯМИ",
    "СЕЙЧАС НЕ ТОРГУЕМ",
    "НАБЛЮДЕНИЕ (БЕЗ СДЕЛКИ)",
    "👁 НАБЛЮДЕНИЕ",
    "WATCH",
)

_WATCH_END_MARKERS = (
    "👀 КЛЮЧЕВОЙ",
    "🛑 ИНВАЛИДАЦИЯ",
    "💬 ПРОСТЫМИ",
    "👶 КАК",
    "📊 QE",
    "📌",
    "📡",
    "🤝",
    "⚠️",
    "🚨",
)


# Известные тикеры — нужны чтобы фильтровать «watch levels» которые на
# самом деле просто новостные булеты (типа «Fibermaxxing → benefits»),
# случайно попавшие в блок наблюдения через chain-of-thought leak от
# Bull/Bear агента.
_KNOWN_TICKERS = {
    # Крипта
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "TON", "TRX",
    "AVAX", "DOT", "LINK", "MATIC", "LTC", "BCH", "USDT", "USDC",
    # Equity / индексы
    "SPY", "QQQ", "IWM", "DIA", "VIX", "SPX", "NDX", "DJI", "GLD",
    "SLV", "TLT", "HYG", "DXY", "USO", "VWO", "EFA", "S&P",
    # Watch для производных
    "ES=F", "NQ=F", "GC=F", "CL=F",
}

_TRIGGER_KEYWORDS = (
    "ПРОБОЙ", "ЗАКРЫТИЕ", "ПРОБИВ", "ОТКРОЕМ", "ШОРТ", "LONG", "SHORT",
    "BREAKOUT", "FLIP", "BREAK", "ПОДДЕРЖК", "СОПРОТИВЛЕНИ",
    "MA50", "MA200", "EMA", "SMA",
)


def _is_valid_watch_level(item: dict) -> bool:
    """Watch-уровень должен содержать тикер + либо цену, либо триггер.
    
    Иначе это мусор: AI-агент в chain-of-thought пишет «Fibermaxxing →
    benefits…», синт случайно копирует булет в блок наблюдения, и юзер
    видит на кнопке «Стратегия» эти заметки вместо реальных уровней.
    Фильтр не пропускает строки без явного тикера/цены/триггера.
    """
    symbol_upper = (item.get("symbol") or "").strip().upper()
    level = (item.get("level") or "").strip()
    note = (item.get("note") or "").strip()
    full_text = f"{symbol_upper} {level} {note}".upper()
    
    has_ticker = any(t in symbol_upper for t in _KNOWN_TICKERS) or any(
        re.search(rf"\b{re.escape(t)}\b", full_text) for t in _KNOWN_TICKERS
    )
    has_price = bool(re.search(r"\$\s*\d|\d{2,}\s*(?:USD|usd|долл)", full_text))
    has_trigger = any(t in full_text for t in _TRIGGER_KEYWORDS)
    
    # Sanity: должна быть либо комбинация (тикер + цена/триггер), либо
    # тикер с явным числом. Голые текстовые булеты без чисел и тикеров
    # — отбрасываем.
    if has_ticker and (has_price or has_trigger):
        return True
    if has_price and has_trigger:
        return True
    # Явный анти-паттерн: новостной булет с «benefits / loses / not market»
    # — это leak Bull-агента.
    leak_markers = ("BENEFITS", "LOSES?", "NOT MARKET", "MAYBE CRITICS")
    if any(m in full_text for m in leak_markers):
        return False
    return False


def extract_watch_levels(report_text: str) -> list[dict]:
    """Парсит блок «📊 СЕЙЧАС НЕ ТОРГУЕМ — СЛЕДИМ ЗА УРОВНЯМИ» / «👁 НАБЛЮДЕНИЕ»
    из Synth-выхода. Возвращает список {symbol, level, note} — то же что
    рендерится в _render_trade_plan_from_json.
    """
    clean = _strip_markup(report_text)
    upper = clean.upper()

    start_idx = -1
    header_len = 0
    for header in _WATCH_HEADERS:
        i = upper.find(header)
        if i != -1 and (start_idx == -1 or i < start_idx):
            start_idx = i
            header_len = len(header)
    if start_idx == -1:
        return []

    body = clean[start_idx + header_len : start_idx + header_len + 1200]
    end_idx = len(body)
    for em in _WATCH_END_MARKERS:
        i = body.find(em)
        if i != -1 and i < end_idx:
            end_idx = i
    body = body[:end_idx]

    items: list[dict] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not (line.startswith("•") or line.startswith("-") or line.startswith("—") or line.startswith("–")):
            continue
        cleaned = _clean_line(line.lstrip("•-—– \t"))
        if len(cleaned) < 3:
            continue
        # Формат «<sym> | <level> | <note>» из render. Может прийти и без pipe
        # (free-form bullet от модели).
        parts = [p.strip() for p in cleaned.split("|") if p.strip()]
        if len(parts) >= 2:
            items.append({
                "symbol": parts[0][:32],
                "level": parts[1][:64],
                "note": " ".join(parts[2:])[:200],
            })
        else:
            items.append({"symbol": "", "level": "", "note": cleaned[:240]})
    
    # Фильтруем мусор от агента который протёк в watch-блок (новостные
    # булеты без цен/тикеров). Без этого юзер видит «Fibermaxxing →
    # benefits» в качестве условия флипа — хуже чем пустой блок.
    cleaned_items = [it for it in items if _is_valid_watch_level(it)]
    return cleaned_items[:6]


def _extract_symbol(label: str) -> str:
    cleaned = _clean_line(label)
    if not cleaned:
        return ""
    # Сначала ищем тикер: «BTC CASH» → BTC, не CASH. До этого было наоборот —
    # любая фраза с CASH сворачивалась в symbol="CASH" даже если рядом стоял
    # реальный тикер. Только если ни тикера, ни paren-нотации не нашли —
    # фолбэчим на CASH (для строк типа «CASH CASH | …»).
    paren_match = re.search(r"\(([A-Z]{1,10}(?:=F)?)\)", cleaned)
    if paren_match:
        return paren_match.group(1)
    match = _SYMBOL_RE.search(cleaned)
    if match:
        sym = match.group(1)
        # «BTC CASH» → BTC, но «CASH» сам по себе → CASH (а не «CASH»→tail).
        return sym
    if "CASH" in cleaned.upper():
        return "CASH"
    return cleaned[:24]


def _normalize_direction(value: str) -> str:
    upper = _clean_line(value).upper()
    if any(token in upper for token in ("LONG", "BUY", "БЫЧ")):
        return "LONG"
    if any(token in upper for token in ("SHORT", "SELL", "МЕДВ", "ШОРТ")):
        return "SHORT"
    if "CASH" in upper or "ВНЕ РЫНКА" in upper:
        return "CASH"
    if "WATCH" in upper or "НАБЛЮД" in upper:
        return "WATCH"
    return upper[:20]


def _strip_field_prefix(text: str, prefixes: tuple[str, ...]) -> str:
    """
    "горизонт 7 дней" -> "7 дней". "триггер: Пробой $94." -> "Пробой $94.".

    Pipe-style chunks come in either as `"<field> <value>"` or
    `"<field>: <value>"`. Renderer (`_plan_line`) re-prepends "горизонт " /
    "триггер ", so we MUST strip the field word here — otherwise we get
    "горизонт горизонт 7 дней".
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return cleaned
    lower = cleaned.lower()
    for prefix in prefixes:
        if lower.startswith(prefix):
            cut = cleaned[len(prefix):]
            return cut.lstrip(" :\t-—–").strip()
    if ":" in cleaned:
        return cleaned.split(":", 1)[-1].strip()
    return cleaned


def _parse_pipe_plan_line(line: str) -> Optional[dict]:
    cleaned = _clean_line(line)
    if "|" not in cleaned:
        return None

    parts = [part.strip() for part in cleaned.split("|") if part.strip()]
    if len(parts) < 2:
        return None

    direction = _normalize_direction(parts[1])
    field_chunks = parts[2:]
    if direction not in {"LONG", "SHORT", "CASH", "WATCH"}:
        # Real-world Synth-выход бывает «BTC CASH | триггер пробой $X» —
        # тикер и направление слиплись в parts[0]. Раньше парсер бросал
        # такие строки, юзер видел пустой план. Берём direction из parts[0]
        # как фолбэк, а parts[1:] — это уже поля (entry/stop/trigger).
        direction = _normalize_direction(parts[0])
        if direction not in {"LONG", "SHORT", "CASH", "WATCH"}:
            return None
        field_chunks = parts[1:]

    # Label-приоритет: если label явно говорит CASH/WATCH/ВНЕ РЫНКА — это
    # watch-уровень, даже если в trigger-чанке встретилось «→ откроем LONG».
    # Без этого parts[1]="триггер пробой $X → откроем LONG" парсится как
    # direction=LONG, и юзер видит фантомную сделку BTC LONG без вход/стоп/цели.
    label_upper = parts[0].upper()
    if direction in {"LONG", "SHORT"} and any(
        x in label_upper for x in ("CASH", "WATCH", "ВНЕ РЫНКА", "НАБЛЮД")
    ):
        if "CASH" in label_upper or "ВНЕ РЫНКА" in label_upper:
            direction = "CASH"
        else:
            direction = "WATCH"
        # parts[1] было ошибочно прочитано как direction. На самом деле это
        # data-поле (обычно trigger). Возвращаем его в обработку field_chunks,
        # иначе watch-уровень потеряет текст триггера.
        field_chunks = parts[1:]

    plan = {
        "label": parts[0],
        "symbol": _extract_symbol(parts[0]),
        "direction": direction,
        "entry": None,
        "stop": None,
        "target": None,
        "horizon": "",
        "trigger": "",
        "rr": "",
        "size": "",
    }

    for chunk in field_chunks:
        chunk_clean = _clean_line(chunk)
        lower = chunk_clean.lower()
        if "вход" in lower or "entry" in lower:
            price_match = re.search(r"\$?([\d.,KkКк]+)", chunk_clean)
            if price_match:
                plan["entry"] = _parse_price(price_match.group(1))
        elif "стоп" in lower or "stop" in lower:
            price_match = re.search(r"\$?([\d.,KkКк]+)", chunk_clean)
            if price_match:
                plan["stop"] = _parse_price(price_match.group(1))
        elif "цель" in lower or "target" in lower or "тейк" in lower:
            price_match = re.search(r"\$?([\d.,KkКк]+)", chunk_clean)
            if price_match:
                plan["target"] = _parse_price(price_match.group(1))
        elif "горизонт" in lower or "horizon" in lower:
            plan["horizon"] = _strip_field_prefix(chunk_clean, ("горизонт", "horizon"))
        elif "триггер" in lower or "trigger" in lower:
            plan["trigger"] = _strip_field_prefix(chunk_clean, ("триггер", "trigger"))
        elif "r/r" in lower:
            plan["rr"] = chunk_clean.replace("R/R", "").replace("r/r", "").strip(": ")
        elif "размер" in lower or "size" in lower:
            plan["size"] = _strip_field_prefix(chunk_clean, ("размер", "size"))

    return plan


def _extract_synth_section(report_text: str) -> str:
    """Извлечь секцию Synth (от ВЕРДИКТ до дисклеймера)."""
    clean = _strip_markup(report_text)
    markers = (
        "ВЕРДИКТ И ТОРГОВЫЙ ПЛАН",
        "ИТОГОВЫЙ СИНТЕЗ",
        "ВЕРДИКТ СУДЬИ",
    )
    for marker in markers:
        idx = clean.upper().find(marker.upper())
        if idx != -1:
            end_markers = ("Честно о боте", "🤝 Честно", "⚠️ Не является", "─" * 20)
            end_idx = len(clean)
            for em in end_markers:
                e = clean.lower().find(em.lower(), idx)
                if e != -1:
                    end_idx = min(end_idx, e)
            return clean[idx:end_idx]
    return ""


_PLAN_BLOCK_END_MARKERS = (
    "👀",  # Точки наблюдения
    "💬",  # Простыми словами
    "📌",  # Эффекты 2-го порядка
    "📡",  # Режим рынка
    "🤝",  # Честно о боте
    "⚠️",  # Дисклеймер
    "🚨",  # Алерты
)


def _extract_plan_subsection(synth_section: str) -> str:
    """
    Из секции Synth выделяем именно блок «📋 Торговый план» — от заголовка
    до первого из followup-маркеров (👀 Точки наблюдения, 💬 Простыми
    словами, 📌 Эффекты, и т.п.). Если заголовка нет — отдаём всю секцию
    Synth как fallback (старое поведение), но с тем же ранним стопом
    на followup-маркерах.
    """
    if not synth_section:
        return ""

    plan_markers = ("📋 Торговый план", "ТОРГОВЫЙ ПЛАН", "Торговый план")
    start_idx = -1
    for marker in plan_markers:
        i = synth_section.find(marker)
        if i != -1 and (start_idx == -1 or i < start_idx):
            start_idx = i
    block = synth_section[start_idx:] if start_idx != -1 else synth_section

    end_idx = len(block)
    for marker in _PLAN_BLOCK_END_MARKERS:
        i = block.find(marker)
        if i != -1 and i < end_idx:
            end_idx = i
    return block[:end_idx]


def _extract_pipe_plans(report_text: str) -> list[dict]:
    plans: list[dict] = []

    # 1) | формат: разрешаем себе пробежаться по всему отчёту, но только
    # по строкам с pipe — других не трогаем.
    for raw_line in _strip_markup(report_text).splitlines():
        line = raw_line.strip()
        if "|" not in line:
            continue
        if not any(marker in line.upper() for marker in ("LONG", "SHORT", "CASH", "WATCH", "ВНЕ РЫНКА", "НАБЛЮД")):
            continue
        plan = _parse_pipe_plan_line(line)
        if plan:
            plans.append(plan)

    # 2) Block-стиль (Актив/Направление/Вход/...). Раньше парсер шёл по
    # ВСЕЙ Synth-секции и подсасывал bullets из «👀 Точки наблюдения» и
    # «📌 Эффекты», превращая их в фиктивные планы. Теперь жёстко
    # обрезаем до блока «📋 Торговый план» и стопаемся на followup-маркерах.
    synth_section = _extract_synth_section(report_text)
    plan_subsection = _extract_plan_subsection(synth_section)
    if not plan_subsection:
        return plans

    current_plan: Optional[dict] = None
    for raw_line in plan_subsection.splitlines():
        # | формат уже обработан pipe-парсером выше. Если оставить эти
        # строки block-парсеру — он стартует НОВЫЙ пустой план «BTC LONG»
        # (по startswith("btc")) поверх уже распарсенного, дедуп их не
        # сольёт (entry/target/stop разные), и юзер видит фантомные дубли.
        if "|" in raw_line:
            if current_plan and current_plan.get("symbol"):
                plans.append(current_plan)
                current_plan = None
            continue

        line = _clean_line(raw_line)
        if not line or len(line) < 4:
            if current_plan and current_plan.get("symbol"):
                plans.append(current_plan)
                current_plan = None
            continue

        lower = line.lower()

        # «Триггер LONG …» — это monitoring-точка, а не новый план,
        # даже если идёт на отдельной строке с маркером.
        if lower.startswith("триггер") and ("long" in lower or "short" in lower):
            continue

        is_bullet = lower.startswith("•") or lower.startswith("-")
        is_known_asset = any(lower.startswith(token) for token in ("btc", "eth", "sol", "spy", "qqq", "wti", "gold", "cash", "usd"))

        if is_bullet or is_known_asset:
            if current_plan and current_plan.get("symbol"):
                plans.append(current_plan)

            label = line.lstrip("•- ").strip()
            direction = "CASH"
            if any(x in label.upper() for x in ("LONG", "BUY")):
                direction = "LONG"
            elif any(x in label.upper() for x in ("SHORT", "SELL", "МЕДВ")):
                direction = "SHORT"

            sym_match = _SYMBOL_RE.search(label)
            sym = sym_match.group(1) if sym_match else label[:12]

            current_plan = {
                "label": label,
                "symbol": sym,
                "direction": direction,
                "entry": None,
                "stop": None,
                "target": None,
                "horizon": "",
                "trigger": "",
                "rr": "",
                "size": "",
            }

        elif current_plan is not None:
            if any(x in lower for x in ("вход", "entry", "стоп", "stop", "цель", "target", "тейк", "rr", "r/r", "размер", "size", "горизонт", "horizon", "триггер", "trigger")):
                parts_split = line.split(":", 1)
                if len(parts_split) >= 2:
                    field = parts_split[0].lower().strip()
                    val = parts_split[1].strip()
                    price_match = re.search(r"\$?([\d.,KkКк]+)", val)
                    p = _parse_price(price_match.group(1)) if price_match else None

                    if any(x in field for x in ("вход", "entry")):
                        current_plan["entry"] = p
                    elif any(x in field for x in ("стоп", "stop")):
                        current_plan["stop"] = p
                    elif any(x in field for x in ("цель", "target", "тейк")):
                        current_plan["target"] = p
                    elif "rr" in field:
                        current_plan["rr"] = val
                    elif "размер" in field or "size" in field:
                        current_plan["size"] = val
                    elif "горизонт" in field or "horizon" in field:
                        current_plan["horizon"] = val
                    elif "триггер" in field or "trigger" in field:
                        current_plan["trigger"] = val

    if current_plan and current_plan.get("symbol"):
        plans.append(current_plan)

    # Намеренно НЕ накатываем generic-fallback `trigger_patterns` поверх
    # планов: раньше он подсасывал «Триггер LONG: Пробой $2350 → …»
    # из «👀 Точки наблюдения» и приклеивал его к ETH CASH / SPY CASH,
    # ломая каждый план. Если конкретный план явно не имеет триггера —
    # пусть так и остаётся, лучше пусто чем чужой триггер.
    return plans


def _extract_block_plans(report_text: str) -> list[dict]:
    lines = [_clean_line(line) for line in _strip_markup(report_text).splitlines()]
    plans: list[dict] = []
    current: Optional[dict] = None

    for line in lines:
        if not line:
            if current and current.get("symbol"):
                plans.append(current)
            current = None
            continue

        lower = line.lower()
        if lower.startswith("актив:") or lower.startswith("asset:"):
            if current and current.get("symbol"):
                plans.append(current)
            label = line.split(":", 1)[1].strip()
            current = {
                "label": label,
                "symbol": _extract_symbol(label),
                "direction": "",
                "entry": None,
                "stop": None,
                "target": None,
                "horizon": "",
                "trigger": "",
                "rr": "",
                "size": "",
            }
            continue

        if not current:
            continue

        if lower.startswith("направление:") or lower.startswith("direction:"):
            current["direction"] = _normalize_direction(line.split(":", 1)[1])
        elif lower.startswith("вход:") or lower.startswith("entry:"):
            current["entry"] = _parse_price(line.split(":", 1)[1])
        elif lower.startswith("стоп:") or lower.startswith("stop:"):
            current["stop"] = _parse_price(line.split(":", 1)[1])
        elif lower.startswith("цель:") or lower.startswith("target:") or lower.startswith("тейк:"):
            current["target"] = _parse_price(line.split(":", 1)[1])
        elif lower.startswith("горизонт:") or lower.startswith("horizon:"):
            current["horizon"] = line.split(":", 1)[1].strip()
        elif lower.startswith("триггер:") or lower.startswith("trigger:"):
            current["trigger"] = line.split(":", 1)[1].strip()
        elif lower.startswith("r/r:"):
            current["rr"] = line.split(":", 1)[1].strip()
        elif lower.startswith("размер:") or lower.startswith("size:"):
            current["size"] = line.split(":", 1)[1].strip()

    if current and current.get("symbol"):
        plans.append(current)

    normalized: list[dict] = []
    for plan in plans:
        direction = _normalize_direction(plan.get("direction", ""))
        if direction not in {"LONG", "SHORT", "CASH", "WATCH"}:
            continue
        plan["direction"] = direction
        normalized.append(plan)
    return normalized


def extract_trade_plans(report_text: str) -> list[dict]:
    plans = _extract_pipe_plans(report_text) + _extract_block_plans(report_text)
    unique: list[dict] = []
    seen: set[tuple] = set()

    for plan in plans:
        key = (
            plan.get("symbol") or plan.get("label"),
            plan.get("direction"),
            round(plan.get("entry") or 0, 4),
            round(plan.get("target") or 0, 4),
            round(plan.get("stop") or 0, 4),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(plan)

    return unique


_MONITORING_HEADERS = (
    "👀 ТОЧКИ НАБЛЮДЕНИЯ",
    "ТОЧКИ НАБЛЮДЕНИЯ",
    "👀 Точки наблюдения",
    "Точки наблюдения",
)

_MONITORING_END_MARKERS = (
    "💬",
    "📌",
    "📡",
    "🤝",
    "⚠️",
    "🚨",
    "📊",
    "📋",
    "🎯",
    "🧠",
    "📜",
)


def _extract_monitoring_block_bullets(report_text: str) -> list[str]:
    """
    Достаём bullets из явной секции «👀 Точки наблюдения:». Эта секция
    есть в Speechwriter-выходе и юзер ожидает увидеть её КАК ЕСТЬ
    в дайджесте. Старая `extract_monitoring_points` ловила только
    «Ключевой триггер ...» и «Триггер LONG ...» — а целые bullet-листы
    типа «данные по CPI и ставке ФРС», «BTC $78,000 пробой вверх»
    тупо терялись.
    """
    clean = _strip_markup(report_text)
    upper = clean.upper()

    start_idx = -1
    for header in _MONITORING_HEADERS:
        i = upper.find(header.upper())
        if i != -1 and (start_idx == -1 or i < start_idx):
            start_idx = i + len(header)
    if start_idx == -1:
        return []

    block = clean[start_idx:start_idx + 1200]

    end_idx = len(block)
    for marker in _MONITORING_END_MARKERS:
        i = block.find(marker)
        if i != -1 and i < end_idx:
            end_idx = i
    block = block[:end_idx]

    bullets: list[str] = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not (line.startswith("•") or line.startswith("-") or line.startswith("—") or line.startswith("–")):
            continue
        cleaned = _clean_line(line)
        if len(cleaned) < 4:
            continue
        bullets.append(cleaned)
    return bullets


def extract_monitoring_points(report_text: str) -> list[str]:
    points: list[str] = []

    points.extend(_extract_monitoring_block_bullets(report_text))
    points.append(_extract_single_line(report_text, "Ключевой триггер для пересмотра"))
    points.append(_extract_single_line(report_text, "Следующий уровень для мониторинга"))

    clean = _strip_markup(report_text)
    trigger_lines = re.findall(r"(Триггер\s+(?:LONG|SHORT)[^.\n]{0,180})", clean, re.IGNORECASE)
    points.extend(trigger_lines[:3])
    return _unique_lines([point for point in points if point])


def _plan_line(plan: dict) -> str:
    direction = (plan.get("direction") or "").upper().strip()
    symbol = (plan.get("symbol") or plan.get("label") or "Актив").strip()

    # Synth иногда выдаёт {"symbol":"CASH","direction":"CASH"} как placeholder
    # «остальное держим в кеше». Без разыменовывания в дайджесте это выглядит
    # как «CASH CASH | триггер …» — явный визуальный баг.
    if symbol.upper() == "CASH" and direction == "CASH":
        head = "Вне рынка"
    else:
        head = f"{symbol} {direction}".strip() if direction else symbol

    chunks = [head]
    if plan.get("entry") is not None:
        chunks.append(f"вход {_format_price(plan['entry'])}")
    if plan.get("target") is not None:
        chunks.append(f"цель {_format_price(plan['target'])}")
    if plan.get("stop") is not None:
        chunks.append(f"стоп {_format_price(plan['stop'])}")
    if plan.get("horizon"):
        chunks.append(f"горизонт {plan['horizon']}")
    if plan.get("trigger"):
        chunks.append(f"триггер {plan['trigger']}")
    if plan.get("rr"):
        chunks.append(f"R/R {plan['rr']}")
    if plan.get("size"):
        chunks.append(f"{plan['size']}")
    return " | ".join(chunks)


def _extract_key_trigger(report_text: str) -> str:
    """Extract key trigger from report (multiple patterns).

    Старая версия бежала по всему отчёту и часто хватала «триггер LONG» из раунда
    Bull/Bear вместо Synth `key_trigger`. Сначала ищем в секции Synth, потом
    fallback на весь отчёт (чтобы не ломать старые форматы).
    """
    patterns = [
        r"ключевой\s+триггер[^.\n]{0,200}",
        r"триггер\s+(?:l?ong|short)[^.\n]{0,200}",
        r"пробой\s+\$?[\d.,Kk]+[^.\n]{0,100}",
        r"мониторинг[уа][^\n]{0,150}",
    ]
    synth_section = _extract_synth_section(report_text)
    haystacks = [synth_section] if synth_section else []
    haystacks.append(_strip_markup(report_text))
    for haystack in haystacks:
        for pattern in patterns:
            match = re.search(pattern, haystack, re.IGNORECASE)
            if match:
                val = _clean_line(match.group(0))
                if len(val) > 5:
                    return val[:200]
    return ""


def _try_parse_synth_json(report_text: str) -> list[dict]:
    """Попытка распарсить JSON если Synth выдал структурированный ответ."""
    synth_section = _extract_synth_section(report_text)
    if not synth_section:
        return []
    
    import json as _json
    
    # Ищем JSON в секции Synth
    for block in re.findall(r'\{[^{}]*"plans"[^{}]*\}', synth_section, re.DOTALL):
        plans = []
        for pm in re.findall(r'\{[^{}]*\}', block):
            try:
                plan_data = _json.loads(pm)
                if plan_data.get("symbol"):
                    plans.append({
                        "label": plan_data.get("symbol", ""),
                        "symbol": plan_data.get("symbol", ""),
                        "direction": plan_data.get("direction", "CASH"),
                        "entry": float(plan_data["entry"]) if plan_data.get("entry") else None,
                        "stop": float(plan_data["stop"]) if plan_data.get("stop") else None,
                        "target": float(plan_data["target"]) if plan_data.get("target") else None,
                        "rr": plan_data.get("rr", ""),
                        "size": plan_data.get("size", ""),
                        "trigger": plan_data.get("trigger", ""),
                    })
            except Exception:
                continue
        if plans:
            return plans
    return []


def _split_actionable_and_watch(plans: list[dict]) -> tuple[list[dict], list[dict]]:
    """Делит план-список на actionable (LONG/SHORT/конкретный CASH) и
    демоутится-в-watch (двунаправленный/абстрактный CASH-триггер).
    LONG/SHORT всегда actionable. Только CASH/WAIT/FLAT с плохим триггером
    падают в watch.
    """
    actionable: list[dict] = []
    watch: list[dict] = []
    for plan in plans or []:
        if not isinstance(plan, dict):
            continue
        unactionable, reason = _is_unactionable_cash_plan(plan)
        if unactionable:
            trigger_txt = str(plan.get("trigger") or "").strip()
            watch.append({
                "symbol": (plan.get("symbol") or plan.get("label") or "?").upper(),
                "level": "",
                "note": trigger_txt or reason,
            })
        else:
            actionable.append(plan)
    return actionable, watch


def _extract_invalidation(report_text: str) -> str:
    """Synth-блок `🛑 ИНВАЛИДАЦИЯ: …` — что отменит весь сценарий."""
    synth_section = _extract_synth_section(report_text)
    if synth_section:
        match = re.search(r"ИНВАЛИДАЦИЯ\s*[:：]\s*([^\n]+)", synth_section, re.IGNORECASE)
        if match:
            return _clean_line(match.group(1))[:240]
    return ""


def build_digest_context(report_text: str, source_news: str = "") -> dict:
    verdict = extract_verdict(report_text)
    plans = extract_trade_plans(report_text)

    # Fallback: try to parse JSON if plans still empty
    if not plans:
        json_plans = _try_parse_synth_json(report_text)
        if json_plans:
            plans = json_plans

    # Демоут двунаправленных/абстрактных CASH-планов в watch-уровни.
    # На Telegram-UI «BTC CASH | пробой $X вниз ИЛИ выше $X» выглядит как
    # план-сделка (с маркером CASH, как сделка-в-кеше), но это watch-уровень
    # без направления. Юзер видит «3 плана» хотя ни одного нет.
    actionable, demoted_watch = _split_actionable_and_watch(plans)
    extracted_watch = extract_watch_levels(report_text)
    watch_levels = extracted_watch + demoted_watch

    monitoring_points = extract_monitoring_points(report_text)
    # Speechwriter рендерит «🧠 Почему: …», старый Synth писал «Потому что: …».
    # Ищем СТРОГО в Synth-секции — иначе «Почему» ловит Bull's
    # «Это неверно потому что: …» из раунда 2 и вставляет возражения Bull вверху
    # дайджеста. Для обратной совместимости fallback на весь отчёт если
    # секция не найдена.
    synth_section = _extract_synth_section(report_text)
    if synth_section:
        verdict_reason = (
            _extract_single_line(synth_section, "Почему")
            or _extract_single_line(synth_section, "Потому что")
            or _extract_single_line(synth_section, "reason")
        )
    else:
        verdict_reason = (
            _extract_single_line(report_text, "Почему")
            or _extract_single_line(report_text, "Потому что")
        )
    verdict_reason = _strip_leading_punct(verdict_reason or "")

    key_trigger = _extract_key_trigger(report_text)
    invalidation = _extract_invalidation(report_text)
    monitoring_level = ""
    plain_language = extract_plain_language(report_text) or _clean_line(source_news)[:320]
    plain_language = _strip_leading_punct(plain_language)
    eli5 = extract_eli5(report_text)

    return {
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS.get(verdict, VERDICT_LABELS["NEUTRAL"]),
        "verdict_emoji": VERDICT_EMOJIS.get(verdict, VERDICT_EMOJIS["NEUTRAL"]),
        "verdict_reason": verdict_reason,
        "key_trigger": key_trigger,
        "invalidation": invalidation,
        "monitoring_level": monitoring_level,
        "monitoring_points": monitoring_points,
        "plain_language": plain_language,
        "eli5": eli5,
        "plans": actionable,
        "watch_levels": watch_levels,
        "full_report": report_text or "",
    }


def format_digest_cache_summary(context: dict, max_plans: int = 4) -> str:
    lines = [f"Вердикт: {context.get('verdict_label', VERDICT_LABELS['NEUTRAL'])}"]

    reason = context.get("verdict_reason")
    if reason:
        lines.append(f"Почему: {reason}")

    plans = context.get("plans") or []
    if plans:
        lines.append("План:")
        for plan in plans[:max_plans]:
            lines.append(f"- {_plan_line(plan)}")
    else:
        lines.append("План: явной сделки нет, работаем только от триггеров наблюдения.")

    monitoring_points = context.get("monitoring_points") or []
    if monitoring_points:
        lines.append("Точки наблюдения:")
        for point in monitoring_points[:3]:
            lines.append(f"- {point}")

    plain_language = context.get("plain_language")
    if plain_language:
        lines.append(f"Простыми словами: {plain_language}")

    return "\n".join(lines)


def format_digest_telegram_summary(
    context: dict,
    *,
    stars: str,
    pct: int,
    timestamp: str,
    max_plans: int = 4,
) -> str:
    verdict = context.get("verdict", "NEUTRAL")
    verdict_label = context.get("verdict_label", VERDICT_LABELS["NEUTRAL"])
    verdict_emoji = context.get("verdict_emoji", VERDICT_EMOJIS["NEUTRAL"])

    lines = [
        "📊 *DIALECTIC EDGE — ЕЖЕДНЕВНЫЙ ДАЙДЖЕСТ*",
        f"🕒 {timestamp}",
        "",
        f"🎯 *ВЕРДИКТ:* {verdict_emoji} *{verdict_label}*",
        f"📊 Сигнал: {stars} ({pct}%)",
    ]

    reason = context.get("verdict_reason")
    if reason:
        lines.extend(["", f"🧠 *Почему:* {reason}"])

    # Торговый план
    plans = context.get("plans") or []
    lines.append("")
    lines.append("📋 *ТОРГОВЫЙ ПЛАН:*")
    
    if plans:
        for plan in plans[:max_plans]:
            lines.append(f"• {_plan_line(plan)}")
    else:
        # Если планов нет — показываем точки наблюдения и ключевые уровни
        key_trigger = context.get("key_trigger")
        monitoring_level = context.get("monitoring_level")
        monitoring_points = context.get("monitoring_points") or []
        
        if key_trigger:
            lines.append(f"⏳ {key_trigger}")
        
        if monitoring_level:
            lines.append(f"👀 Уровень мониторинга: {monitoring_level}")
        
        if monitoring_points:
            lines.append("")
            lines.append("*Точки наблюдения:*")
            for point in monitoring_points[:3]:
                lines.append(f"• {point}")
        
        if not plans and not key_trigger and not monitoring_level and not monitoring_points:
            lines.append("⏳ Ждём подтверждения по триггерам")

    # Макро данные из plain_language если нет планов
    plain_language = context.get("plain_language")
    if plain_language:
        lines.extend(["", f"💬 *Простыми словами:* {plain_language[:300]}"])

    lines.extend(["", "📎 Полный анализ + дебаты — в файлах ниже."])
    return "\n".join(lines)
