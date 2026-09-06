from __future__ import annotations

from typing import Any, Optional

import httpx
from loguru import logger

from hunt.utils.http import fetch_json
from hunt.utils.ratelimit import RateLimiter

BASE = "https://public-api.birdeye.so"
BAD_TAGS = {"bundler", "dev"}


class Birdeye:
    def __init__(self, client: httpx.AsyncClient, api_key: str, db) -> None:
        self.client = client
        self.api_key = api_key
        self.db = db
        self.limiter = RateLimiter(rate_per_sec=0.25, burst=2)

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def _cu_budget_ok(self, cost: int) -> bool:
        from hunt.config import get_settings

        s = get_settings()
        used = await self.db.kv_get_int(f"birdeye_cu:{time_today()}")
        return used + cost <= s.birdeye_daily_cu_budget

    async def top_traders(
        self,
        mint: str,
        time_frame: str = "24h",
        limit: int = 10,
        offset: int = 0,
        min_realized_pnl_usd: float = 50.0,
        min_trades: int = 3,
    ) -> list[dict[str, Any]]:
        if not self.available:
            return []
        cost = 6
        if not await self._cu_budget_ok(cost):
            logger.debug("birdeye daily CU budget exhausted")
            return []
        await self.limiter.acquire()
        headers = {"X-API-KEY": self.api_key, "x-chain": "solana"}
        params = {
            "address": mint,
            "time_frame": time_frame,
            "offset": offset,
            "limit": limit,
        }
        data = await fetch_json(
            self.client, "GET", f"{BASE}/defi/v2/tokens/top_traders",
            params=params, headers=headers,
        )
        if data is None:
            return []
        await self.db.kv_bump_daily("birdeye_cu", cost)
        items = ((data.get("data") or {}).get("items")) or []
        owners: list[dict[str, Any]] = []
        for it in items:
            owner = it.get("owner")
            if not owner:
                continue
            tags = set(it.get("tags") or [])
            if tags & BAD_TAGS:
                logger.debug("skipping {} tagged {}", owner[:8], sorted(tags))
                continue
            realized = float(it.get("realizedPnl") or 0)
            trades_n = int(it.get("trade") or 0)
            if realized < min_realized_pnl_usd or trades_n < min_trades:
                logger.debug(
                    "skipping {} pnl=${:.0f} trades={} below threshold",
                    owner[:8], realized, trades_n,
                )
                continue
            owners.append({
                "owner": owner,
                "volume_usd": float(it.get("volumeUsd") or 0),
                "trades": trades_n,
                "realized_pnl_usd": realized,
                "tags": sorted(tags),
            })
        return owners


def time_today() -> str:
    import time as _t

    return _t.strftime("%Y-%m-%d")
