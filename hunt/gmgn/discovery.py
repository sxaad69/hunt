from __future__ import annotations

import time
from dataclasses import dataclass

from loguru import logger

from hunt.config import get_settings
from hunt.db.database import Database
from hunt.gmgn.client import GmgnClient


@dataclass
class DiscoveredWallet:
    wallet: str
    mint: str
    realized_profit: float


class GmgnDiscovery:
    def __init__(self, db: Database, client: GmgnClient) -> None:
        self.s = get_settings()
        self.db = db
        self.client = client

    async def top_wallets_for_token(self, mint: str) -> list[DiscoveredWallet]:
        rows = await self.client.token_top_traders(
            mint, limit=self.s.gmgn_traders_per_token
        )
        out: list[DiscoveredWallet] = []
        for r in rows:
            addr = r.get("address")
            pnl = float(r.get("realized_profit") or 0)
            if not addr or pnl <= 0 or r.get("is_suspicious"):
                continue
            out.append(DiscoveredWallet(addr, mint, pnl))
        return out

    async def discover_once(self) -> tuple[int, list[tuple[str, int]]]:
        tokens = await self.db.universe_tokens()
        targets = tokens[: self.s.gmgn_discovery_tokens_per_cycle]
        seen: dict[str, int] = {}
        new_wallets = 0
        today = time.strftime("%Y-%m-%d", time.gmtime())

        for row in targets:
            mint = row["mint"]
            found = await self.top_wallets_for_token(mint)
            for i, dw in enumerate(found):
                seen[dw.wallet] = seen.get(dw.wallet, 0) + 1
                await self.db.save_edge(
                    dw.wallet, mint, today, "gmgn_toptrader", i + 1
                )
                wrow = await self.db.get_wallet(dw.wallet)
                if wrow is None:
                    sym = row["symbol"] if "symbol" in row.keys() else None
                    await self.db.upsert_wallet(
                        dw.wallet, f"gmgn_toptrader:{sym}", first_token=mint
                    )
                    new_wallets += 1
            await asyncio_sleep_small()

        await self.db.commit_edges()
        multi = [(w, n) for w, n in seen.items() if n > 1]
        logger.info(
            "gmgn discovery: {} tokens swept → {} wallets ({} multi-coin)",
            len(targets), len(seen), len(multi),
        )
        return new_wallets, multi


async def asyncio_sleep_small() -> None:
    import asyncio

    await asyncio.sleep(1.0)
