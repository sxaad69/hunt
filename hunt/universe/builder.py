from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx
from loguru import logger

from hunt.config import get_settings
from hunt.db.database import Database
from hunt.universe.ohlcv import OhlcvFetcher
from hunt.utils.concurrency import GlobalRateBucket


@dataclass
class DayWinner:
    day: str
    mint: str
    symbol: str | None
    gain_pct: float
    volume_usd: float


class UniverseBuilder:
    def __init__(self, db: Database) -> None:
        s = get_settings()
        self.s = s
        self.db = db
        self.http = httpx.AsyncClient()
        self.bucket = GlobalRateBucket(s.rpc_global_rps * 0.5, 8)
        self.ohlcv = OhlcvFetcher(self.http, db, self.bucket)

    async def close(self) -> None:
        await self.http.aclose()

    async def token_pool(self) -> list[tuple[str, str | None]]:
        pool: dict[str, str | None] = {}
        for r in await self.db.db.execute_fetchall(
            "SELECT mint, symbol FROM seen_tokens ORDER BY last_hot_ts DESC LIMIT 400"
        ):
            if _is_solana_address(r[0]):
                pool[r[0]] = r[1]
        try:
            r = await self.http.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={
                    "vs_currency": "usd",
                    "category": "solana-ecosystem",
                    "order": "market_cap_desc",
                    "per_page": 150,
                    "page": 1,
                },
                timeout=20,
            )
            if r.status_code == 200:
                for t in r.json():
                    addr = t.get("platforms", {}).get("solana")
                    if addr and _is_solana_address(addr):
                        pool.setdefault(addr, (t.get("symbol") or "?").upper())
        except Exception as e:
            logger.debug("coingecko pool fetch failed: {}", e)
        return list(pool.items())[:500]

    def rank_daily_winners(self, candles_by_mint: dict[str, list]) -> list[DayWinner]:
        days = self.s.universe_days
        now = int(time.time())
        today_start = now - (now % 86400)
        per_day: dict[str, list[DayWinner]] = {}

        for mint, candles in candles_by_mint.items():
            for i in range(1, len(candles)):
                c_prev, c = candles[i - 1], candles[i]
                if c_prev.ts < today_start - days * 86400:
                    continue
                if c.volume_usd < self.s.universe_min_day_volume_usd:
                    continue
                gain = (c.close / c_prev.open - 1.0) * 100.0
                if gain < self.s.universe_min_day_gain_pct:
                    continue
                day = time.strftime("%Y-%m-%d", time.gmtime(c.ts))
                per_day.setdefault(day, []).append(
                    DayWinner(day, mint, None, gain, c.volume_usd)
                )

        winners: list[DayWinner] = []
        for day, items in sorted(per_day.items()):
            items.sort(key=lambda w: w.gain_pct, reverse=True)
            winners.extend(items[: self.s.universe_top_k_per_day])
        return winners

    async def build_once(self) -> int:
        pool = await self.token_pool()
        logger.info("universe: fetching candles for {} tokens", len(pool))

        candles_by_mint: dict[str, list] = {}
        sym_by_mint: dict[str, str | None] = {}

        async def work(mint: str, sym: str | None):
            candles = await self.ohlcv.fetch_candles(mint, self.s.universe_days)
            if candles:
                candles_by_mint[mint] = candles
                sym_by_mint[mint] = sym

        await asyncio.gather(*(work(m, s) for m, s in pool), return_exceptions=True)

        winners = self.rank_daily_winners(candles_by_mint)
        for w in winners:
            if not w.symbol:
                w.symbol = sym_by_mint.get(w.mint)
            await self.db.save_universe_day(w.day, w.mint, w.symbol, w.gain_pct, w.volume_usd)
        await self.db.commit_universe()

        uniq = len({w.mint for w in winners})
        logger.info(
            "universe built: {} winner-slots across {} days → {} unique tokens",
            len(winners), len({w.day for w in winners}), uniq,
        )
        return uniq

    async def run(self) -> None:
        while True:
            try:
                await self.build_once()
            except Exception as e:
                logger.exception("universe loop error: {}", e)
            await asyncio.sleep(self.s.universe_refresh_h * 3600)


def _is_solana_address(addr: str) -> bool:
    if not (32 <= len(addr) <= 44):
        return False
    allowed = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    return all(c in allowed for c in addr)


async def run_universe(db: Database) -> None:
    b = UniverseBuilder(db)
    try:
        await b.run()
    finally:
        await b.close()
