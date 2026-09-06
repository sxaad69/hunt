from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx
from loguru import logger

from hunt.utils.ratelimit import RateLimiter

BASE = "https://api.dexscreener.com"


@dataclass
class HotToken:
    mint: str
    symbol: str
    dex_id: str
    pair_address: str
    price_usd: float
    liquidity_usd: float
    vol24h_usd: float
    change24h_pct: float
    pair_created_at: int | None
    market_cap: float = 0.0


class DexScreener:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.limiter = RateLimiter(rate_per_sec=1.5, burst=5)

    async def _get(self, path: str) -> Optional[dict | list]:
        await self.limiter.acquire()
        from hunt.utils.http import fetch_json

        return await fetch_json(self.client, "GET", BASE + path)

    async def candidate_mints(self, limit: int = 80) -> list[str]:
        mints: list[str] = []
        boosts = await self._get("/token-boosts/top/v1")
        if isinstance(boosts, list):
            for item in boosts:
                if item.get("chainId") == "solana":
                    addr = item.get("tokenAddress")
                    if addr and addr not in mints:
                        mints.append(addr)
        profiles = await self._get("/token-profiles/latest/v1")
        if isinstance(profiles, list):
            for item in profiles:
                if item.get("chainId") == "solana":
                    addr = item.get("tokenAddress")
                    if addr and addr not in mints:
                        mints.append(addr)
        return mints[:limit]

    async def pairs_for_mints(self, mints: list[str]) -> dict[str, HotToken]:
        out: dict[str, HotToken] = {}
        for i in range(0, len(mints), 30):
            chunk = mints[i : i + 30]
            data = await self._get("/tokens/v1/solana/" + ",".join(chunk))
            if not isinstance(data, list):
                continue
            for pair in data:
                ht = self._parse_pair(pair)
                if ht is None:
                    continue
                cur = out.get(ht.mint)
                if cur is None or ht.liquidity_usd > cur.liquidity_usd:
                    out[ht.mint] = ht
        return out

    def _parse_pair(self, pair: dict) -> Optional[HotToken]:
        try:
            base = pair.get("baseToken") or {}
            mint = base.get("address")
            if not mint:
                return None
            liq = ((pair.get("liquidity") or {}).get("usd")) or 0.0
            vol = ((pair.get("volume") or {}).get("h24")) or 0.0
            chg = ((pair.get("priceChange") or {}).get("m24h")) or 0.0
            created = pair.get("pairCreatedAt")
            market_cap = pair.get("marketCap") or pair.get("fdv") or 0.0
            return HotToken(
                mint=mint,
                symbol=base.get("symbol") or "?",
                dex_id=pair.get("dexId") or "?",
                pair_address=pair.get("pairAddress") or "",
                price_usd=float(pair.get("priceUsd") or 0),
                liquidity_usd=float(liq),
                vol24h_usd=float(vol),
                change24h_pct=float(chg),
                pair_created_at=int(created / 1000) if created else None,
                market_cap=float(market_cap),
            )
        except Exception as e:
            logger.debug("pair parse failed: {}", e)
            return None

    async def price_for_mint(self, mint: str) -> tuple[float, float]:
        pairs = await self.pairs_for_mints([mint])
        ht = pairs.get(mint)
        if not ht:
            return 0.0, 0.0
        return ht.price_usd, ht.liquidity_usd

    async def prices_batch(self, mints: list[str]) -> dict[str, float]:
        pairs = await self.pairs_for_mints(mints)
        return {m: ht.price_usd for m, ht in pairs.items()}
