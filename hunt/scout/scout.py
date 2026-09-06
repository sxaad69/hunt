from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx
from loguru import logger

from hunt.config import get_settings
from hunt.db.database import Database
from hunt.notify.base import Notifier
from hunt.scout.birdeye import Birdeye
from hunt.scout.dexscreener import DexScreener, HotToken
from hunt.scout.pumpfun import PumpFun, looks_like_pump


@dataclass
class ScoutStats:
    tokens_scanned: int = 0
    hot_tokens: int = 0
    candidates_added: int = 0


class Scout:
    def __init__(self, db: Database, notifier: Notifier) -> None:
        s = get_settings()
        self.s = s
        self.db = db
        self.notifier = notifier
        self.client = httpx.AsyncClient()
        self.ds = DexScreener(self.client)
        self.birdeye = Birdeye(self.client, s.birdeye_api_key, db)
        self.pumpfun = PumpFun(self.client)

    async def close(self) -> None:
        await self.client.aclose()

    def _is_hot(self, ht: HotToken) -> bool:
        if ht.liquidity_usd < self.s.hot_token_min_liquidity_usd:
            return False
        pumped = (
            ht.vol24h_usd >= self.s.hot_token_min_vol24h_usd
            and abs(ht.change24h_pct) >= self.s.hot_token_min_change24h_pct
        )
        fresh = (
            ht.pair_created_at is not None
            and (time.time() - ht.pair_created_at) < 48 * 3600
            and ht.vol24h_usd >= 20000
        )
        return pumped or fresh

    async def scout_once(self) -> ScoutStats:
        stats = ScoutStats()
        mints = await self.ds.candidate_mints()
        stats.tokens_scanned = len(mints)
        pairs = await self.ds.pairs_for_mints(mints)
        hot: list[HotToken] = []
        for mint, ht in pairs.items():
            first_time = await self.db.mark_seen_token(
                ht.mint, ht.symbol, ht.liquidity_usd, ht.vol24h_usd,
                ht.change24h_pct, ht.pair_created_at,
            )
            if not first_time:
                continue
            if self._is_hot(ht):
                hot.append(ht)
        stats.hot_tokens = len(hot)
        logger.info("scout: {} scanned, {} new hot tokens", stats.tokens_scanned, len(hot))

        added_wallets: set[str] = set()
        for ht in hot[: self.s.scout_max_new_tokens_per_run]:
            sources: list[tuple[str, list[str]]] = []
            traders: list[dict] = []
            for offset in (0, 10):
                traders.extend(await self.birdeye.top_traders(ht.mint, offset=offset))
            seen_owners: set[str] = set()
            clean: list[str] = []
            for t in traders:
                o = t["owner"]
                if o not in seen_owners:
                    seen_owners.add(o)
                    clean.append(o)
            if clean:
                sources.append(("birdeye", clean))
            if looks_like_pump(ht.dex_id):
                buyers = await self.pumpfun.early_buyers(ht.mint)
                if buyers:
                    sources.append(("pumpfun", buyers))
            for source_name, wallets in sources:
                for w in wallets:
                    if w in added_wallets:
                        continue
                    await self.db.upsert_wallet(w, f"{source_name}:{ht.symbol}", first_token=ht.mint)
                    added_wallets.add(w)
        stats.candidates_added = len(added_wallets)
        if hot:
            top = ", ".join(f"${h.symbol}" for h in hot[:5])
            await self.notifier.send(
                f"🔍 scout: {len(hot)} new hot token(s) [{top}] → "
                f"{stats.candidates_added} candidate wallet(s)"
            )
        return stats

    async def run(self) -> None:
        while True:
            try:
                await self.scout_once()
            except Exception as e:
                logger.exception("scout loop error: {}", e)
            await asyncio.sleep(self.s.scout_interval_s)


async def run_scout(db: Database, notifier: Notifier) -> None:
    scout = Scout(db, notifier)
    try:
        await scout.run()
    finally:
        await scout.close()
