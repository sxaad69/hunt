from __future__ import annotations

import asyncio
import time

import httpx
from loguru import logger

from hunt.config import get_settings
from hunt.utils.concurrency import GlobalRateBucket
from hunt.utils.http import post_json_rpc
from hunt.utils.swaps import deltas_from_ws_notification, extract_swap_for_owner

WSOL = "So11111111111111111111111111111111111111112"


async def resolve_pool_dexscreener(mint: str, client: httpx.AsyncClient | None = None, retries: int = 2) -> str | None:
    own = client is None
    c = client or httpx.AsyncClient(timeout=10)
    try:
        for attempt in range(retries + 1):
            try:
                r = await c.get(f"https://api.dexscreener.com/token-pairs/v1/solana/{mint}")
                if r.status_code == 429:
                    await asyncio.sleep(15 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    return None
                pairs = r.json() or []
                best = max(
                    pairs,
                    key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0),
                    default=None,
                )
                if not best:
                    return None
                dex = (best.get("dexId") or "").lower()
                if dex and dex not in {"pumpswap", "pump-swap", "raydium"}:
                    return None
                return best.get("pairAddress")
            except Exception:
                await asyncio.sleep(2 * (attempt + 1))
        return None
    finally:
        if own:
            await c.aclose()


class PriceReplayer:
    def __init__(self) -> None:
        s = get_settings()
        self.s = s
        self.bucket = GlobalRateBucket(rate_per_sec=6.0, burst=12)
        self.client = httpx.AsyncClient(timeout=20)

    async def _rpc(self, method: str, params: list):
        await self.bucket.acquire()
        return await post_json_rpc(self.client, self.s.rpc_http, method, params)

    async def series_for_pool(self, pool: str, since_ts: int, max_txs: int = 350) -> list[dict]:
        resp = await self._rpc("getSignaturesForAddress", [pool, {"limit": 1000}])
        if not resp or "result" not in resp:
            return []
        sigs = []
        for e in resp["result"] or []:
            bt = e.get("blockTime") or 0
            if e.get("err"):
                continue
            if bt >= since_ts - 600:
                sigs.append((e["signature"], bt))
            if len(sigs) >= max_txs:
                break
        sigs.reverse()

        pts: dict[int, list[float]] = {}
        sem = asyncio.Semaphore(3)

        async def fetch_swap(sig: str, bt: int):
            async with sem:
                tx = await self._rpc("getTransaction", [
                    sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                          "maxSupportedTransactionVersion": 0},
                ])
            if not tx or not tx.get("result"):
                return
            r = tx["result"]
            meta = r.get("meta") or {}
            if meta.get("err"):
                return
            deltas = deltas_from_ws_notification({
                "transaction": {"transaction": r.get("transaction"), "meta": meta}
            })
            for (owner, mint), delta in list(deltas.token_deltas.items()):
                if mint == WSOL or delta <= 0:
                    continue
                swaps = extract_swap_for_owner(deltas, owner)
                for sw in swaps:
                    if sw.token_amount > 0 and sw.sol_amount > 0:
                        minute = bt - (bt % 60)
                        pts.setdefault(minute, []).append(sw.sol_amount / sw.token_amount)
                break

        chunk = 9
        for i in range(0, len(sigs), chunk):
            await asyncio.gather(*(fetch_swap(s, b) for s, b in sigs[i:i+chunk]))

        candles: list[dict] = []
        for minute in sorted(pts):
            prices = pts[minute]
            candles.append({
                "time": minute,
                "open": prices[0], "close": prices[-1],
                "high": max(prices), "low": min(prices),
                "volume": 0.0,
            })
        return candles


async def resolve_pool_gmgn(mint: str) -> str | None:
    from hunt.config import get_settings
    from hunt.gmgn.client import GmgnClient

    client = GmgnClient(get_settings().gmgn_api_key)
    data = await client._run(["token", "info", "--chain", "sol", "--address", mint])
    if not data:
        return None
    pool = data.get("migrated_pool") or data.get("biggest_pool_address")
    return pool or None


async def resolve_pool_any(mint: str, ds_client: httpx.AsyncClient | None = None) -> str | None:
    pool = await resolve_pool_dexscreener(mint, ds_client)
    if pool:
        return pool
    return await resolve_pool_gmgn(mint)


async def pool_price_series(
    mint: str, created_ts: int, client: httpx.AsyncClient | None = None
) -> list[dict]:
    rp = PriceReplayer()
    pool = await resolve_pool_any(mint, client)
    if not pool:
        return []
    sol_usd = await _sol_price_usd()
    raw = await rp.series_for_pool(pool, created_ts, max_txs=300)
    if sol_usd <= 0:
        return []
    for c in raw:
        for k in ("open", "high", "low", "close"):
            c[k] = c[k] * sol_usd
    return raw


_sol_cache: tuple[float, float] = (0.0, 0.0)


async def _sol_price_usd() -> float:
    global _sol_cache
    now = time.time()
    if now - _sol_cache[1] > 120:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    "https://api.dexscreener.com/tokens/v1/solana/"
                    + WSOL
                )
                pairs = r.json() or []
                price = float(pairs[0].get("priceUsd") or 0) if pairs else 0.0
                if price > 0:
                    _sol_cache = (price, now)
        except Exception:
            pass
    return _sol_cache[0]
