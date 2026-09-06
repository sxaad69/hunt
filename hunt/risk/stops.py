from __future__ import annotations

import asyncio
import time

import httpx
from loguru import logger

from hunt.config import get_settings
from hunt.db.database import Database
from hunt.exec.executor import TradeExecutor
from hunt.notify.base import Notifier
from hunt.scout.dexscreener import DexScreener


class StopsMonitor:
    def __init__(self, db: Database, notifier: Notifier, executor: TradeExecutor) -> None:
        self.s = get_settings()
        self.db = db
        self.notifier = notifier
        self.executor = executor
        self.ds = DexScreener(httpx.AsyncClient())

    async def run(self) -> None:
        while True:
            try:
                await self.check_once()
            except Exception as e:
                logger.exception("stops loop error: {}", e)
            await asyncio.sleep(self.s.stops_interval_s)

    async def check_once(self) -> None:
        mode = "PAPER" if self.s.dry_run else "LIVE"
        positions = await self.db.get_open_positions(mode)
        if not positions:
            return
        mints = list({p["mint"] for p in positions})
        prices = await self.ds.prices_batch(mints)
        for pos in positions:
            price = prices.get(pos["mint"], 0.0)
            if price <= 0:
                continue
            await self.db.update_position_peak(pos["id"], price)
            entry = pos["entry_price_usd"] or 0.0
            if entry <= 0:
                continue
            change_pct = (price / entry - 1.0) * 100.0
            peak = max(pos["peak_price_usd"] or entry, price)

            # priority: SL -> migration -> TP -> trailing -> max_hold
            if change_pct <= self.s.stop_loss_pct:
                await self.executor.exit_position(pos, "stop_loss")
                continue
            # migration / graduation: if pump curve dead and no price via DexScreener fallback check
            # we treat price==0 already skipped above; migration exit is handled by backing price source
            if change_pct >= self.s.take_profit_pct:
                await self.executor.exit_position(pos, "take_profit")
                continue
            if (
                self.s.trailing_stop_pct > 0
                and peak > entry
                and price <= peak * (1 - self.s.trailing_stop_pct / 100.0)
                and (peak / entry - 1.0) * 100.0 >= self.s.take_profit_pct * 0.5
            ):
                await self.executor.exit_position(pos, "trailing_stop")
                continue
            if (time.time() - pos["opened_ts"]) > self.s.max_hold_hours * 3600:
                await self.executor.exit_position(pos, "max_hold")
