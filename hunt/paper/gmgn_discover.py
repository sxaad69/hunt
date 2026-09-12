from __future__ import annotations

import asyncio
import time

from loguru import logger

from hunt.gmgn.client import GmgnClient
from hunt.gmgn.flow import parse_token_flow


def _ts_ms(raw) -> int:
    try:
        n = int(raw or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return int(time.time() * 1000)
    if n < 1e12:
        return n * 1000
    return n


async def gmgn_discover_loop(stop_event: asyncio.Event, on_coin, interval_s: float = 45.0) -> None:
    client = GmgnClient()
    while not stop_event.is_set():
        offered = 0
        try:
            trades = await client.smart_money_trades(limit=80)
            now_due = int((time.time() - 90.0) * 1000)
            for t in trades:
                mint = t.mint or ""
                if t.side.lower() != "buy" or not mint.endswith("pump"):
                    continue
                if t.amount_usd < 40:
                    continue
                await on_coin({
                    "mint": mint,
                    "symbol": t.symbol or "?",
                    "created_timestamp": now_due,
                    "_from_gmgn": "smartmoney",
                    "_gmgn_wallet": t.wallet,
                })
                offered += 1
            rows = await client.trenches(limit=40, min_smart=1)
            for row in rows:
                mint = row.get("address") or ""
                if not mint.endswith("pump"):
                    continue
                coin = {
                    "mint": mint,
                    "symbol": row.get("symbol") or "?",
                    "created_timestamp": _ts_ms(row.get("created_timestamp")),
                    "_from_gmgn": "trenches",
                    "_gmgn_flow": parse_token_flow(row),
                }
                await on_coin(coin)
                offered += 1
            logger.info("gmgn-discover offered={}", offered)
        except Exception as e:
            logger.info("gmgn-discover idle — {}", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
