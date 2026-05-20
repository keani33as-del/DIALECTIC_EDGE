"""Sniper limit-order levels for the `/signal` screen."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from aiogram import F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

logger = logging.getLogger(__name__)

SNIPING_CALLBACK_PREFIX = "sniping"
DEFAULT_SNIPING_CAPITAL = 123.0
MIN_SNIPING_RR = 1.2
SNIPING_SYMBOLS = ("BTC", "ETH", "SOL", "BNB", "XRP")


@dataclass(frozen=True)
class SniperLevel:
    price: float
    side: str  # "SUPPORT" or "RESISTANCE"
    timeframes: tuple[str, ...]
    score: float = 0.0
    touches: int = 1
    bars_ago: int = 0


@dataclass(frozen=True)
class SniperPlan:
    asset: str
    direction: str
    entry: float
    stop: float
    target: float
    rr_ratio: float
    distance_pct: float
    timeframes: tuple[str, ...]
    quantity: float
    position_value: float
    risk_amount: float
    risk_pct: float
    score: float = 0.0


@dataclass(frozen=True)
class SniperReport:
    asset: str | None
    direction: str | None
    current_price: float | None
    signal_score: int | None
    plans: list[SniperPlan] = field(default_factory=list)
    reason: str = ""


def sniping_callback_data(user_id: int, capital: float = DEFAULT_SNIPING_CAPITAL) -> str:
    return f"{SNIPING_CALLBACK_PREFIX}:{int(user_id)}:{float(capital):.2f}"


def parse_sniping_callback_data(data: str | None) -> tuple[int, float] | None:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != SNIPING_CALLBACK_PREFIX:
        return None
    try:
        return int(parts[1]), max(10.0, float(parts[2].replace(",", ".")))
    except (TypeError, ValueError):
        return None


def _fmt_money(value: float) -> str:
    if value >= 1000:
        return f"${value:,.0f}"
    if value >= 10:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:,.3f}"
    return f"${value:,.5f}"


def _fmt_qty(value: float) -> str:
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _merge_levels(levels: list[SniperLevel], *, tolerance_pct: float = 0.35) -> list[SniperLevel]:
    if not levels:
        return []
    out: list[SniperLevel] = []
    for level in sorted(levels, key=lambda lv: lv.price):
        if not out:
            out.append(level)
            continue
        prev = out[-1]
        close = abs(level.price - prev.price) / max(prev.price, 1e-9) * 100 <= tolerance_pct
        if close and level.side == prev.side:
            total_score = max(prev.score + level.score, 1e-9)
            merged_price = ((prev.price * max(prev.score, 1.0)) + (level.price * max(level.score, 1.0))) / (max(prev.score, 1.0) + max(level.score, 1.0))
            out[-1] = SniperLevel(
                price=merged_price,
                side=prev.side,
                timeframes=tuple(sorted(set(prev.timeframes + level.timeframes))),
                score=total_score,
                touches=prev.touches + level.touches,
                bars_ago=min(prev.bars_ago or level.bars_ago, level.bars_ago or prev.bars_ago),
            )
        else:
            out.append(level)
    return out


def _levels_from_sr(sr_by_tf: dict[str, Any], current_price: float) -> list[SniperLevel]:
    raw: list[SniperLevel] = []
    for tf, sr in sr_by_tf.items():
        resistances = getattr(sr, "resistances", []) or []
        supports = getattr(sr, "supports", []) or []
        for lv in supports:
            price = float(getattr(lv, "price", 0) or 0)
            if price <= 0 or price >= current_price:
                continue
            raw.append(SniperLevel(
                price=price,
                side="SUPPORT",
                timeframes=(tf,),
                score=float(getattr(lv, "score", 1.0) or 1.0),
                touches=int(getattr(lv, "touches", 1) or 1),
                bars_ago=int(getattr(lv, "bars_ago", 0) or 0),
            ))
        for lv in resistances:
            price = float(getattr(lv, "price", 0) or 0)
            if price <= current_price:
                continue
            raw.append(SniperLevel(
                price=price,
                side="RESISTANCE",
                timeframes=(tf,),
                score=float(getattr(lv, "score", 1.0) or 1.0),
                touches=int(getattr(lv, "touches", 1) or 1),
                bars_ago=int(getattr(lv, "bars_ago", 0) or 0),
            ))
    return _merge_levels(raw)


def build_sniper_plans_for_asset(
    *,
    asset: str,
    direction: str,
    current_price: float,
    sr_by_tf: dict[str, Any],
    capital: float = DEFAULT_SNIPING_CAPITAL,
    atr: float | None = None,
    realized_vol_pct: float | None = None,
    max_plans: int = 3,
) -> list[SniperPlan]:
    if current_price <= 0 or capital <= 0:
        return []
    direction = (direction or "").upper()
    if direction not in {"LONG", "SHORT"}:
        return []

    levels = _levels_from_sr(sr_by_tf, current_price)
    supports = sorted([lv for lv in levels if lv.price < current_price], key=lambda lv: lv.price, reverse=True)
    resistances = sorted([lv for lv in levels if lv.price > current_price], key=lambda lv: lv.price)
    candidates = supports if direction == "LONG" else resistances
    if not candidates:
        return []

    from core.dynamic_risk import DynamicRiskManager

    risk_mgr = DynamicRiskManager()
    plans: list[SniperPlan] = []
    buffer_pct = max(0.0025, min(0.015, ((atr or 0.0) / current_price) * 0.15))

    for candidate in candidates[: max_plans * 2]:
        entry = candidate.price
        if direction == "LONG":
            lower = [lv for lv in supports if lv.price < entry * 0.999]
            stop = (lower[0].price * (1 - buffer_pct)) if lower else entry * (1 - max(0.006, buffer_pct * 2))
            higher_levels = sorted([lv for lv in levels if lv.price > entry * 1.001], key=lambda lv: lv.price)
            target = higher_levels[0].price if higher_levels else entry + (entry - stop) * 2.0
            dyn_direction = "BUY"
            distance_pct = (entry - current_price) / current_price * 100
            risk_distance = entry - stop
            reward_distance = target - entry
        else:
            higher = [lv for lv in resistances if lv.price > entry * 1.001]
            stop = (higher[0].price * (1 + buffer_pct)) if higher else entry * (1 + max(0.006, buffer_pct * 2))
            lower_levels = sorted([lv for lv in levels if lv.price < entry * 0.999], key=lambda lv: lv.price, reverse=True)
            target = lower_levels[0].price if lower_levels else entry - (stop - entry) * 2.0
            dyn_direction = "SELL"
            distance_pct = (entry - current_price) / current_price * 100
            risk_distance = stop - entry
            reward_distance = entry - target

        if risk_distance <= 0 or reward_distance <= 0:
            continue
        rr = reward_distance / risk_distance
        if rr < MIN_SNIPING_RR:
            continue

        sizing = risk_mgr.calculate_position_size(
            capital=capital,
            entry_price=entry,
            stop_price=stop,
            atr=0,
            regime="UPTREND" if direction == "LONG" else "DOWNTREND",
            correlation_count=0,
            realized_vol_pct=float(realized_vol_pct or 0.0),
            direction=dyn_direction,
        )
        if sizing.get("error"):
            continue

        plans.append(SniperPlan(
            asset=asset.upper(),
            direction=direction,
            entry=entry,
            stop=stop,
            target=target,
            rr_ratio=rr,
            distance_pct=distance_pct,
            timeframes=candidate.timeframes,
            quantity=float(sizing.get("quantity") or 0.0),
            position_value=float(sizing.get("position_value") or 0.0),
            risk_amount=float(sizing.get("risk_amount") or 0.0),
            risk_pct=float(sizing.get("risk_pct") or 0.0),
            score=float(candidate.score),
        ))

    plans.sort(key=lambda p: (-p.rr_ratio, abs(p.distance_pct)))
    return plans[:max_plans]


async def _fetch_binance_klines(session: aiohttp.ClientSession, symbol: str, interval: str, limit: int) -> tuple[list[float], list[float]]:
    url = "https://api.binance.com/api/v3/klines"
    async with session.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=aiohttp.ClientTimeout(total=8)) as resp:
        if resp.status != 200:
            return [], []
        data = await resp.json()
    highs = [float(k[2]) for k in data]
    lows = [float(k[3]) for k in data]
    return highs, lows


async def _fetch_okx_klines(session: aiohttp.ClientSession, asset: str, interval: str, limit: int) -> tuple[list[float], list[float]]:
    bar_map = {"15m": "15m", "1h": "1H", "1d": "1D"}
    inst_id = f"{asset.upper()}-USDT-SWAP"
    async with session.get(
        "https://www.okx.com/api/v5/market/candles",
        params={"instId": inst_id, "bar": bar_map.get(interval, interval), "limit": str(limit)},
        timeout=aiohttp.ClientTimeout(total=8),
    ) as resp:
        if resp.status != 200:
            return [], []
        payload = await resp.json()
    rows = list(reversed(payload.get("data") or []))
    highs = [float(k[2]) for k in rows]
    lows = [float(k[3]) for k in rows]
    return highs, lows


async def _fetch_klines(session: aiohttp.ClientSession, asset: str, interval: str, limit: int) -> tuple[list[float], list[float]]:
    symbol = f"{asset.upper()}USDT"
    try:
        highs, lows = await _fetch_binance_klines(session, symbol, interval, limit)
        if highs and lows:
            return highs, lows
    except Exception as exc:
        logger.debug("Binance klines failed for %s %s: %s", asset, interval, exc)
    try:
        return await _fetch_okx_klines(session, asset, interval, limit)
    except Exception as exc:
        logger.debug("OKX klines failed for %s %s: %s", asset, interval, exc)
        return [], []


async def fetch_multitimeframe_sr(asset: str, current_price: float, fallback_daily: dict | None = None) -> dict[str, Any]:
    from core.support_resistance import compute_sr_levels

    tf_specs = {
        "M15": ("15m", 240, 3, 0.25, 36.0),
        "H1": ("1h", 240, 4, 0.35, 48.0),
        "D1": ("1d", 250, 5, 0.50, 30.0),
    }
    out: dict[str, Any] = {}
    try:
        async with aiohttp.ClientSession() as session:
            tasks = {
                tf: _fetch_klines(session, asset, interval, limit)
                for tf, (interval, limit, _lookback, _tol, _half) in tf_specs.items()
            }
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for (tf, (_interval, _limit, lookback, tolerance, halflife)), result in zip(tf_specs.items(), results):
            if isinstance(result, Exception):
                continue
            highs, lows = result
            if len(highs) >= 30 and len(lows) >= 30:
                out[tf] = compute_sr_levels(
                    highs,
                    lows,
                    current_price=current_price,
                    lookback=lookback,
                    tolerance_pct=tolerance,
                    recency_halflife=halflife,
                    num_each_side=3,
                )
    except Exception as exc:
        logger.debug("fetch_multitimeframe_sr failed for %s: %s", asset, exc)

    if "D1" not in out and fallback_daily:
        highs = fallback_daily.get("_highs_daily")
        lows = fallback_daily.get("_lows_daily")
        if isinstance(highs, list) and isinstance(lows, list) and len(highs) >= 30:
            out["D1"] = compute_sr_levels(
                highs,
                lows,
                current_price=current_price,
                lookback=5,
                tolerance_pct=0.5,
                recency_halflife=30.0,
                num_each_side=3,
            )
    return out


def format_sniper_report(report: SniperReport) -> str:
    if not report.plans:
        asset = report.asset or "лучшей сделке"
        direction = f" {report.direction}" if report.direction else ""
        price = f" сейчас {_fmt_money(report.current_price)}" if report.current_price else ""
        reason = report.reason or "ближайшие S/R не дают R/R ≥ 1.2 или мало OHLC данных"
        return (
            "*🎯 Снайпинг /signal*\n\n"
            f"По {asset}{direction}{price} снайперских лимиток нет.\n"
            f"Причина: {reason}.\n\n"
            "Действие: не ставим лимитку. Ждём новый `/signal` позже."
        )

    head = report.plans[0]
    side_word = "откат вниз к поддержке" if head.direction == "LONG" else "вынос вверх к сопротивлению"
    lines = [
        "*🎯 Снайпинг /signal*",
        "",
        f"Актив: *{head.asset}* · направление: *{head.direction}* · score: `{report.signal_score or '?'}`",
        f"Цена сейчас: `{_fmt_money(report.current_price or head.entry)}`. Идея: ждём {side_word}, не бежим market.",
        "",
    ]
    for idx, plan in enumerate(report.plans, 1):
        tf = "+".join(plan.timeframes) if plan.timeframes else "S/R"
        wait = f"{plan.distance_pct:+.2f}%"
        lines.extend([
            f"*{idx}) {plan.direction} limit* `{_fmt_money(plan.entry)}` ({tf}, ожидание {wait})",
            f"   SL `{_fmt_money(plan.stop)}` · TP `{_fmt_money(plan.target)}` · R/R `{plan.rr_ratio:.2f}`",
            f"   Size `{_fmt_money(plan.position_value)}` · qty `{_fmt_qty(plan.quantity)}` · риск `{_fmt_money(plan.risk_amount)}` (`{plan.risk_pct:.2f}%` капитала)",
        ])
    lines.extend([
        "",
        "Если цена не дошла до entry — сделки нет. Это лимитки, не приказ входить по рынку.",
    ])
    return "\n".join(lines)


async def build_live_sniper_report(capital: float = DEFAULT_SNIPING_CAPITAL) -> SniperReport:
    from core.signal_scorer import rank_signals
    from web_search import fetch_realtime_prices

    prices = await fetch_realtime_prices()
    if not prices:
        return SniperReport(None, None, None, None, reason="не удалось получить цены")

    ranked = rank_signals(prices, capital=capital)
    setup = ranked.get("top") or ranked.get("preview_top")
    if setup is None:
        return SniperReport(None, None, None, None, reason="нет tradable-кандидата из `/signal`")

    asset = getattr(setup, "asset", "").upper()
    direction = getattr(setup, "direction", "").upper()
    p = prices.get(asset, {}) if isinstance(prices, dict) else {}
    current_price = float(p.get("price") or getattr(setup, "entry", 0) or 0)
    if asset not in SNIPING_SYMBOLS or current_price <= 0:
        return SniperReport(asset or None, direction or None, current_price or None, getattr(setup, "score", None), reason="актив не торгуется в sniper-листе")

    sr_by_tf = await fetch_multitimeframe_sr(asset, current_price, fallback_daily=p)
    plans = build_sniper_plans_for_asset(
        asset=asset,
        direction=direction,
        current_price=current_price,
        sr_by_tf=sr_by_tf,
        capital=capital,
        atr=p.get("atr_14d"),
        realized_vol_pct=getattr(setup, "sigma_1d_pct", None),
        max_plans=3,
    )
    return SniperReport(
        asset=asset,
        direction=direction,
        current_price=current_price,
        signal_score=getattr(setup, "score", None),
        plans=plans,
        reason="ближайшие S/R не дают R/R ≥ 1.2 или мало OHLC данных",
    )


async def _answer_md(message: Message, text: str) -> None:
    try:
        await message.answer(text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception:
        await message.answer(text)


async def handle_sniping_command(message: Message) -> None:
    text = (message.text or "").strip()
    parts = text.split()
    capital = DEFAULT_SNIPING_CAPITAL
    if len(parts) >= 2:
        try:
            capital = max(10.0, float(parts[1].replace(",", ".")))
        except ValueError:
            pass
    wait_msg = await message.answer("⏳ Считаю снайперские лимитки по S/R...")
    report = await build_live_sniper_report(capital=capital)
    await wait_msg.delete()
    await _answer_md(message, format_sniper_report(report))


async def handle_sniping_callback(callback: CallbackQuery) -> None:
    parsed = parse_sniping_callback_data(callback.data)
    if parsed is None:
        await callback.answer()
        return
    user_id, capital = parsed
    if user_id != callback.from_user.id:
        await callback.answer("Кнопка не с твоего аккаунта", show_alert=True)
        return
    await callback.answer("Считаю снайперские уровни...")
    report = await build_live_sniper_report(capital=capital)
    text = format_sniper_report(report)
    try:
        await callback.message.answer(text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text)


def register_sniping_handlers(dp) -> None:
    dp.message.register(handle_sniping_command, Command("sniping"))
    dp.callback_query.register(handle_sniping_callback, F.data.startswith(f"{SNIPING_CALLBACK_PREFIX}:"))
