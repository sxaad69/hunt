"""Trial round-trip: ONE tiny live curve buy -> hold -> sell. Supervised use only.

The engine stays STOPPED during a trial — this tool is the only thing that
touches the wallet. Each step is a separate invocation so the operator
controls the hold:

    .venv/bin/python tools/trial_roundtrip.py pick [--min-mcap-sol 35] [--max-age-min 10]
    .venv/bin/python tools/trial_roundtrip.py buy --mint MINT --size-sol 0.002
    .venv/bin/python tools/trial_roundtrip.py sell --mint MINT
    .venv/bin/python tools/trial_roundtrip.py status [--mint MINT]

Trial strictness: buy/sell exit non-zero unless the fill venue is "curve"
(AMM fallback is NOT what a trial proves). Guards: buy size capped at
0.01 SOL; sell always sells the entire token balance with close_ata=True.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

import httpx

API = "https://frontend-api-v3.pump.fun/coins"
MAX_BUY_SOL = 0.01


async def pick(min_mcap: float, max_age_min: float) -> int:
    now_ms = time.time() * 1000
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            API,
            params={"offset": 0, "limit": 70, "sort": "created_timestamp",
                    "order": "DESC", "complete": "false"},
            headers={"accept": "application/json"},
        )
        r.raise_for_status()
        coins = r.json() or []
    print(f"{'mint':10s} {'symbol':14s} {'mcap':>8s} {'age':>6s}  name")
    n = 0
    for c in coins:
        mint = c.get("mint") or ""
        if not mint.endswith("pump"):
            continue
        mc = float(c.get("market_cap") or 0)
        age_min = (now_ms - float(c.get("created_timestamp") or 0)) / 60000.0
        if mc < min_mcap or age_min > max_age_min or age_min < 0:
            continue
        print(f"{mint[:10]} {(c.get('symbol') or '?')[:14]:14s} {mc:8.0f} {age_min:5.1f}m  {(c.get('name') or '')[:30]}")
        n += 1
    print(f"candidates: {n} (non-graduated, {min_mcap:.0f}+ SOL, <{max_age_min:.0f}min old)")
    return 0


async def buy(mint: str, size_sol: float) -> int:
    from hunt.exec.live import LiveExecutor

    if not (0 < size_sol <= MAX_BUY_SOL):
        print(f"REFUSING: size {size_sol} outside (0, {MAX_BUY_SOL}]")
        return 2
    ex = LiveExecutor()
    bal = await ex.balance_sol()
    print(f"wallet: {ex.wallet}  balance: {bal:.6f} SOL")
    r = await ex.buy(mint, size_sol)
    if not r or not r.ok:
        print("BUY FAILED (no fill, tokens NOT bought)")
        return 1
    print(f"BUY venue={r.venue} sig={r.signature}")
    print(f"  spent={r.sol_lamports / 1e9:.6f} SOL tokens_raw={r.tokens_raw} decimals={r.decimals}")
    if r.venue != "curve":
        print("NOT CURVE VENUE — trial not proven")
        return 1
    print("CURVE BUY PROVEN")
    return 0


async def sell(mint: str) -> int:
    from hunt.exec.live import LiveExecutor

    ex = LiveExecutor()
    raw = await ex._token_balance_raw(mint)
    print(f"wallet: {ex.wallet}  token balance raw={raw}")
    if raw <= 0:
        print("nothing to sell")
        return 1
    r = await ex.sell(mint, raw, close_ata=True)
    if not r or not r.ok:
        print("SELL FAILED (tokens kept — inspect, do NOT assume flat)")
        return 1
    print(f"SELL venue={r.venue} sig={r.signature}")
    print(f"  received={r.sol_lamports / 1e9:.6f} SOL")
    if r.venue != "curve":
        print("NOT CURVE VENUE — trial not proven")
        return 1
    print("CURVE SELL PROVEN")
    return 0


async def status(mint: str | None) -> int:
    from hunt.exec.live import LiveExecutor

    ex = LiveExecutor()
    print(f"wallet: {ex.wallet}  SOL={await ex.balance_sol():.6f}")
    if mint:
        print(f"{mint[:10]} token raw={await ex._token_balance_raw(mint)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Supervised trial round-trip (tiny, curve-only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pick")
    p.add_argument("--min-mcap-sol", type=float, default=35.0)
    p.add_argument("--max-age-min", type=float, default=10.0)
    b = sub.add_parser("buy")
    b.add_argument("--mint", required=True)
    b.add_argument("--size-sol", type=float, default=0.002)
    s = sub.add_parser("sell")
    s.add_argument("--mint", required=True)
    t = sub.add_parser("status")
    t.add_argument("--mint", default=None)
    args = ap.parse_args()
    if args.cmd == "pick":
        return asyncio.run(pick(args.min_mcap_sol, args.max_age_min))
    if args.cmd == "buy":
        return asyncio.run(buy(args.mint, args.size_sol))
    if args.cmd == "sell":
        return asyncio.run(sell(args.mint))
    return asyncio.run(status(args.mint))


if __name__ == "__main__":
    sys.exit(main())
