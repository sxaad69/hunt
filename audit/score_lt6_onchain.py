#!/usr/bin/env python3
"""On-chain now vs decision-time for paper A fills with Tracker score < 6."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx

from hunt.config import get_settings
from hunt.paper.onchain_intel import fetch_amm_mcap, fetch_curve_mcap, fetch_onchain_top10

DB = "hunt/data/hunt.sqlite3"
API = "https://frontend-api-v3.pump.fun/coins"


def picks() -> list[tuple]:
    c = sqlite3.connect(DB)
    rows = list(c.execute(
        """SELECT p.symbol, p.mint, p.pnl_sol, p.exit_reason, d.market_cap, d.top10, d.tracker_json
           FROM positions p JOIN paper_decisions d ON d.mint=p.mint
           WHERE p.mode='PAPER' AND p.entry_price_sol>0 AND p.entry_price_sol<5e-6"""
    ))
    out = []
    for sym, mint, pnl, ex, mc, t10, tj in rows:
        sc = None
        if tj:
            try:
                sc = int((json.loads(tj) or {}).get("score"))
            except Exception:
                sc = None
        if sc is None or sc >= 6:
            continue
        out.append((sc, sym or "?", mint, float(pnl or 0), ex or "",
                    float(mc or 0), float(t10 or 0)))
    out.sort()
    return out


async def now_mcap(client, rpc, mint: str) -> tuple[float | None, bool]:
    got = await fetch_curve_mcap(client, rpc, mint)
    if got is None:
        return None, False
    on_mc, on_grad = got
    if not on_grad:
        return on_mc, False
    try:
        r = await client.get(f"{API}/{mint}", timeout=8)
        pool = (r.json() or {}).get("pool_address") or ""
    except Exception:
        pool = ""
    amm = await fetch_amm_mcap(client, rpc, pool) if pool else None
    return amm, True


async def main():
    s = get_settings()
    rows = picks()
    print("sc sym          mint     pnl     exit             mc90 t10_90  now_mc  grad t10now fold")
    async with httpx.AsyncClient(timeout=20, headers={"user-agent": "hunt-lab/1"}) as client:
        for sc, sym, mint, pnl, ex, mc, t10 in rows:
            now_mc, grad = await now_mcap(client, s.rpc_http, mint)
            tnow = await fetch_onchain_top10(client, s.rpc_http, mint)
            fold = (now_mc / mc) if (now_mc and mc > 0) else None
            fs = f"{fold:.2f}x" if fold else "—"
            nms = f"{now_mc:.0f}" if now_mc else "—"
            tns = f"{tnow:.0f}" if tnow is not None else "—"
            print(f"{sc} {sym[:12]:12} {mint[:8]} {pnl:+.4f} {ex[:16]:16} "
                  f"{mc:.0f} {t10:.0f}  {nms:>6}  {int(grad)} {tns:>6} {fs}")
            await asyncio.sleep(0.12)


if __name__ == "__main__":
    asyncio.run(main())
