"""Funding-rate screen for top USDT perpetuals."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp
from aiogram.filters import Command
from aiogram.types import Message

logger = logging.getLogger(__name__)

FUNDING_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "LTCUSDT",
)
BINANCE_FUTURES = "https://fapi.binance.com"
OKX_PUBLIC = "https://www.okx.com"


@dataclass(frozen=True)
class FundingRow:
    symbol: str
    rate: float
    mark_price: float | None = None
    index_price: float | None = None
    next_funding_time_ms: int | None = None
    source: str = "Binance"

    @property
    def asset(self) -> str:
        return self.symbol.removesuffix("USDT")

    @property
    def rate_pct(self) -> float:
        return self.rate * 100


def classify_funding(rate: float) -> tuple[str, str]:
    pct = rate * 100
    abs_pct = abs(pct)
    if pct >= 0.08:
        return "🔴", "аномальный contango: long crowded, short-edge"
    if pct >= 0.03:
        return "🟠", "contango: лонгисты платят, шорты получают funding"
    if pct >= 0.01:
        return "🟡", "умеренный long-bias"
    if pct <= -0.08:
        return "🟢", "аномальный negative funding: толпа в шорте, squeeze-risk"
    if pct <= -0.03:
        return "🟢", "шортисты платят, возможен short-squeeze"
    if pct <= -0.01:
        return "🔵", "умеренный short-bias"
    if abs_pct < 0.01:
        return "⚪", "норма / edge слабый"
    return "⚪", "нейтрально"


def _fmt_next_time(ms: int | None) -> str:
    if not ms:
        return "?"
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        return dt.strftime("%H:%M UTC")
    except Exception:
        return "?"


def format_funding_report(rows: list[FundingRow]) -> str:
    if not rows:
        return (
            "*💸 Funding rates*\n\n"
            "Данных по funding сейчас нет. Binance Futures не ответил или пары недоступны."
        )

    ordered = sorted(rows, key=lambda r: abs(r.rate), reverse=True)
    lines = [
        "*💸 Funding rates — top-10 futures*",
        "",
        "Ставка за 8ч. `+` = лонгисты платят шортистам (contango). Для шортов это carry-edge, но вход всё равно только от уровней.",
        "",
    ]
    for row in ordered:
        emoji, label = classify_funding(row.rate)
        next_time = _fmt_next_time(row.next_funding_time_ms)
        lines.append(f"{emoji} *{row.asset}* `{row.rate_pct:+.4f}%` · {label} · {row.source} · next `{next_time}`")

    long_crowded = [r.asset for r in ordered if r.rate_pct >= 0.03]
    short_crowded = [r.asset for r in ordered if r.rate_pct <= -0.03]
    lines.append("")
    if long_crowded:
        lines.append("Short-watch: " + ", ".join(long_crowded[:5]) + " — positive funding платит за ожидание шорта.")
    if short_crowded:
        lines.append("Squeeze-watch: " + ", ".join(short_crowded[:5]) + " — толпа в шорте, осторожно с поздним SHORT.")
    if not long_crowded and not short_crowded:
        lines.append("Аномалий нет: funding спокойный, edge слабый.")
    return "\n".join(lines)


async def _fetch_one_binance_funding(session: aiohttp.ClientSession, symbol: str) -> FundingRow | None:
    try:
        async with session.get(
            f"{BINANCE_FUTURES}/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            timeout=aiohttp.ClientTimeout(total=6),
        ) as resp:
            if resp.status != 200:
                logger.debug("funding %s status=%s", symbol, resp.status)
                return None
            data = await resp.json()
        return FundingRow(
            symbol=symbol,
            rate=float(data.get("lastFundingRate") or 0.0),
            mark_price=float(data["markPrice"]) if data.get("markPrice") else None,
            index_price=float(data["indexPrice"]) if data.get("indexPrice") else None,
            next_funding_time_ms=int(data["nextFundingTime"]) if data.get("nextFundingTime") else None,
            source="Binance",
        )
    except Exception as exc:
        logger.debug("Binance funding fetch failed for %s: %s", symbol, exc)
        return None


async def _fetch_one_okx_funding(session: aiohttp.ClientSession, symbol: str) -> FundingRow | None:
    asset = symbol.removesuffix("USDT")
    inst_id = f"{asset}-USDT-SWAP"
    try:
        async with session.get(
            f"{OKX_PUBLIC}/api/v5/public/funding-rate",
            params={"instId": inst_id},
            timeout=aiohttp.ClientTimeout(total=6),
        ) as resp:
            if resp.status != 200:
                logger.debug("okx funding %s status=%s", inst_id, resp.status)
                return None
            payload = await resp.json()
        rows = payload.get("data") or []
        if not rows:
            return None
        data = rows[0]
        return FundingRow(
            symbol=symbol,
            rate=float(data.get("fundingRate") or data.get("settFundingRate") or 0.0),
            next_funding_time_ms=int(data["fundingTime"]) if data.get("fundingTime") else None,
            source="OKX",
        )
    except Exception as exc:
        logger.debug("OKX funding fetch failed for %s: %s", symbol, exc)
        return None


async def _fetch_one_funding(session: aiohttp.ClientSession, symbol: str) -> FundingRow | None:
    row = await _fetch_one_binance_funding(session, symbol)
    if row is not None:
        return row
    return await _fetch_one_okx_funding(session, symbol)


async def fetch_funding_rates(symbols: tuple[str, ...] = FUNDING_SYMBOLS) -> list[FundingRow]:
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*[_fetch_one_funding(session, sym) for sym in symbols])
    return [row for row in results if row is not None]


async def handle_funding_command(message: Message) -> None:
    wait_msg = await message.answer("⏳ Тяну funding с Binance Futures...")
    rows = await fetch_funding_rates()
    text = format_funding_report(rows)
    await wait_msg.delete()
    try:
        await message.answer(text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception:
        await message.answer(text)


def register_funding_handlers(dp) -> None:
    dp.message.register(handle_funding_command, Command("funding"))
