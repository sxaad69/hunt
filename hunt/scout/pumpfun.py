from __future__ import annotations

from typing import Optional

import httpx
from loguru import logger

from hunt.utils.http import fetch_json
from hunt.utils.ratelimit import RateLimiter

ENDPOINTS = [
    "https://frontend-api-v3.pump.fun/trades/{mint}?limit=200&offset=0",
    "https://frontend-api.pump.fun/trades/{mint}?limit=200&offset=0",
]


class PumpFun:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.limiter = RateLimiter(rate_per_sec=0.5, burst=2)

    async def early_buyers(self, mint: str, max_buyers: int = 12) -> list[str]:
        await self.limiter.acquire()
        for url_tpl in ENDPOINTS:
            data = await fetch_json(self.client, "GET", url_tpl.format(mint=mint), retries=1)
            if isinstance(data, list) and data:
                return self._extract(data, max_buyers)
            if isinstance(data, dict) and data.get("items"):
                return self._extract(data["items"], max_buyers)
        logger.debug("pumpfun trades unavailable for {}", mint[:8])
        return []

    def _extract(self, trades: list[dict], max_buyers: int) -> list[str]:
        buys = [t for t in trades if t.get("is_buy")]
        buys.sort(key=lambda t: t.get("timestamp") or t.get("unixTime") or 0)
        seen: list[str] = []
        for t in buys:
            user = t.get("user") or t.get("wallet") or t.get("userAddress")
            if user and user not in seen:
                seen.append(user)
            if len(seen) >= max_buyers:
                break
        return seen

    async def is_pump_token(self, mint: str) -> bool:
        return False


def looks_like_pump(dex_id: str) -> bool:
    return dex_id.lower() in {"pumpfun", "pump-fun", "pump_swap", "pumpswap"}
