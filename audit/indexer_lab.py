#!/usr/bin/env python3
"""Side-by-side: pump.fun indexer vs on-chain top10 vs SolanaTracker sniper%.

Sample = fresh curve listings (GET /coins complete=false). Not ACCEPTs.
Run on Linode (Mac NXDOMAINs *.pump.fun).

  .venv/bin/python audit/indexer_lab.py [N]
"""
from __future__ import annotations

import asyncio
import sys
import time

import httpx
from loguru import logger

from hunt.config import get_settings
from hunt.paper.onchain_intel import fetch_onchain_top10
from hunt.utils.solanatracker import check_risk

N = int(sys.argv[1]) if len(sys.argv) > 1 else 40
API = "https://frontend-api-v3.pump.fun/coins"
INDEXER = "https://advanced-indexer.pump.fun/in-memory-coin"


def bucket(idx: float | None, chain: float | None, idx_http: int) -> str:
    if idx_http != 200 or chain is None:
        return "unmeasurable"
    if idx is None:
        idx = 0.0
    if idx <= 0.5 and chain > 75:
        return "RUFUS"
    if idx <= 0.5 and chain > 20:
        return "idx_zero"
    if idx > 75 and chain > 75:
        return "agree_heavy"
    if idx <= 75 and chain <= 75:
        return "agree_clean"
    if idx > 75 and chain <= 75:
        return "idx_hot"
    if idx <= 75 and chain > 75:
        return "idx_miss"
    return "other"


async def listing(client: httpx.AsyncClient, n: int) -> list[dict]:
    out, seen = [], set()
    for off in range(0, 400, 70):
        r = await client.get(
            API,
            params={"offset": off, "limit": 70, "sort": "created_timestamp",
                    "order": "DESC", "complete": "false"},
            headers={"accept": "application/json", "user-agent": "hunt-lab/1"},
            timeout=20,
        )
        if r.status_code != 200:
            logger.warning("listing {} {}", off, r.status_code)
            break
        for c in r.json() or []:
            m = c.get("mint") or ""
            if not m or m in seen:
                continue
            seen.add(m)
            out.append(c)
            if len(out) >= n:
                return out
        await asyncio.sleep(0.3)
    return out


async def indexer(client: httpx.AsyncClient, mint: str) -> tuple[int, dict]:
    try:
        r = await client.get(f"{INDEXER}/{mint}", timeout=8,
                             headers={"accept": "application/json", "user-agent": "hunt-lab/1"})
        if r.status_code != 200:
            return r.status_code, {}
        return 200, r.json() or {}
    except Exception as e:
        return 0, {"_err": str(e)[:80]}


async def one(client: httpx.AsyncClient, coin: dict, rpc: str) -> dict:
    mint = coin["mint"]
    sym = (coin.get("symbol") or "?")[:12]
    http_st, d = await indexer(client, mint)
    chain = await fetch_onchain_top10(client, rpc, mint)
    ok_risk, tr = await check_risk(mint, client)
    idx_top = None
    if http_st == 200:
        try:
            idx_top = float(d.get("top10HoldersPercent") or 0)
        except (TypeError, ValueError):
            idx_top = 0.0
    try:
        idx_snip = int(d.get("sniperCount") or 0) if http_st == 200 else None
    except (TypeError, ValueError):
        idx_snip = None
    try:
        idx_devp = float(d.get("devHoldingsPercent") or 0) if http_st == 200 else None
    except (TypeError, ValueError):
        idx_devp = None
    dev = (d.get("dev") or "") if http_st == 200 else ""
    return {
        "sym": sym, "mint": mint, "http": http_st,
        "idx": idx_top, "chain": chain,
        "idx_snip": idx_snip, "idx_devp": idx_devp,
        "dev": bool(dev), "tracker": tr, "ok_risk": ok_risk,
        "bucket": bucket(idx_top, chain, http_st),
    }


async def main():
    logger.remove()
    s = get_settings()
    rpc = s.rpc_http
    if not s.helius_api_key:
        print("need HUNT_HELIUS_API_KEY")
        sys.exit(1)
    t0 = time.time()
    async with httpx.AsyncClient(timeout=20) as client:
        coins = await listing(client, N)
        print(f"sample {len(coins)} curve listings  complete=false")
        rows = []
        for i, c in enumerate(coins, 1):
            rows.append(await one(client, c, rpc))
            print(f"  {i}/{len(coins)} {rows[-1]['sym']}", flush=True)
    print()
    print(f"{'sym':<12} {'mint':<10} {'idx%':>6} {'chain%':>7} {'Δ':>7} {'bucket':<12} "
          f"{'snipN':>5} {'trk':<22} {'dev%':>5} dev")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["bucket"]] = counts.get(r["bucket"], 0) + 1
        idx_s = f"{r['idx']:.1f}" if r["idx"] is not None else "—"
        ch_s = f"{r['chain']:.1f}" if r["chain"] is not None else "—"
        if r["idx"] is not None and r["chain"] is not None:
            dlt = f"{r['chain'] - r['idx']:+.1f}"
        else:
            dlt = "—"
        sn = str(r["idx_snip"]) if r["idx_snip"] is not None else "—"
        dp = f"{r['idx_devp']:.1f}" if r["idx_devp"] is not None else "—"
        print(f"{r['sym']:<12} {r['mint'][:8]:<10} {idx_s:>6} {ch_s:>7} {dlt:>7} {r['bucket']:<12} "
              f"{sn:>5} {r['tracker']:<22} {dp:>5} {int(r['dev'])}")
    print()
    print("buckets:", "  ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))
    rufus = counts.get("RUFUS", 0)
    print(f"RUFUS-class (idx≈0 & chain>75): {rufus}/{len(rows)}")
    print(f"indexer 200: {sum(1 for r in rows if r['http']==200)}/{len(rows)}  "
          f"dev present: {sum(1 for r in rows if r['dev'])}/{len(rows)}  "
          f"{time.time()-t0:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
