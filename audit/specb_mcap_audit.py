#!/usr/bin/env python3
"""Specimen-B mcap audit — "real picture" of what species-B does AFTER listing.

Target list = pump.fun's own ranked gainers (survivor-biased by design; that's
exactly the population we'd be *entering*). For every target coin we join our
live decisions DB for the pre-graduation reference (90s snapshot = the listing/
entry point), pull the coin's current mcap straight from the /coins fetch, and
compute:

    fold = current SOL mcap / 90s SOL mcap   ("did the run continue after AMM?")

Sorted by fold so the answer to "was the wild-west run at the AMM moment or
after it" is visible at a glance. Deliberately NO on-chain replay: mcap-only,
~0 extra RPC (current mcap already comes with the ranked list).

Buckets by fold:
    <0.2  dusted       the listing WAS the top, coin buried
    0.2-0.8 faded      lost most of its post-90s value
    0.8-1.2 flat       coin is where we'd have entered it
    >1.2 continued     ran ABOVE the entry point after AMM
    >=5   ran away     kept its monster run going

  NOTE: current mcap is a LOWER bound. Peak-then-dump (coin ran +40% then died
  back below 90s) is not captured here — treat near-all-dusted cautiously and
  flag the near-miss slice before concluding.

Usage: python audit/specb_mcap_audit.py [N] [hours] [min_change_x]
   N            top N gainers to audit (default 40)
   hours        how far back "launched today" reaches (default 24)
   min_change_x change-floor of the ranked list (default 10)
"""
import sqlite3
import sys
import time

import httpx

N = int(sys.argv[1]) if len(sys.argv) > 1 else 40
HOURS = int(sys.argv[2]) if len(sys.argv) > 2 else 24
MIN_X = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
BIRTH_USD = 4500.0
DB = "hunt/data/hunt.sqlite3"

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row


def fetch(url, **kw):
    with httpx.Client(timeout=15, headers={"accept": "application/json",
                                           "user-agent": "Mozilla/5.0"}) as cl:
        return cl.get(url, **kw).json()


def sol_price() -> float:
    for url in ("https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT",
                "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"):
        try:
            j = fetch(url)
            p = float(j.get("price") or (j.get("solana") or {}).get("usd") or 0)
            if p > 0:
                return p
        except Exception:
            continue
    return 0.0


def bucket(fold: float) -> str:
    if fold < 0.2:
        return "dusted"
    if fold < 0.8:
        return "faded"
    if fold < 1.2:
        return "flat"
    if fold < 5.0:
        return "continued"
    return "ran away"


def main():
    sol = sol_price()
    now_ms = time.time() * 1000
    coins = {}
    min_usd = BIRTH_USD * MIN_X
    for complete in ("true", "false"):
        for off in range(0, 2001, 100):
            try:
                batch = fetch("https://frontend-api-v3.pump.fun/coins",
                              params={"offset": off, "limit": 100, "sort": "market_cap",
                                      "order": "DESC", "complete": complete})
            except Exception:
                continue
            batch = batch or []
            if not batch or max(float(x.get("usd_market_cap") or 0) for x in batch) < min_usd:
                break
            for x in batch:
                m = x.get("mint")
                if not m or not str(m).endswith("pump"):
                    continue
                usd = float(x.get("usd_market_cap") or 0)
                created = x.get("created_timestamp") or 0
                age_h = (now_ms - created) / 3.6e6
                if 0 < age_h <= HOURS and usd >= min_usd:
                    coins[m] = (x.get("symbol") or "?", m, usd, age_h)

    ranked = sorted(coins.values(), key=lambda t: -t[2])[:N]

    print(f"specimen-B mcap audit: top {len(ranked)} pump.fun gainers (last {HOURS}h, >= {MIN_X:.0f}x)")
    print(f"SOL = ${sol:,.2f}   |   fold = current SOL mcap / 90s snapshot (lower bound; peak-then-dump deferred)")
    if sol <= 0:
        print("WARNING: could not fetch SOL price; post-probs shown as $ only")
    print()
    print(f"{'#':>2s} {'token':12s} {'90s<SOL>':>10s} {'now<$>':>11s} {'now<SOL>':>10s} {'fold':>6s} {'bucket':>9s}  our-system")

    rows = []
    missed = seen = 0
    for i, (sym, mint, usd, age_h) in enumerate(ranked, 1):
        dec = c.execute("SELECT decision, reason, market_cap FROM paper_decisions WHERE mint=?",
                        (mint,)).fetchone()
        pos = c.execute("SELECT status, ROUND(pnl_sol,4), exit_reason FROM positions WHERE mint=?",
                        (mint,)).fetchone()
        if pos:
            sys_note = f"POSITION {pos['status']} {pos['exit_reason'] or ''} {pos[1]:+.4f} SOL"
        elif dec:
            sys_note = f"{dec['decision']}: {dec['reason']}"
        else:
            sys_note = "never seen (MISSED)"
        pre = float(dec["market_cap"]) if dec and dec["market_cap"] else 0.0
        post = usd / sol if sol > 0 else 0.0
        if pre <= 0:
            missed += 1
            rows.append((None, sym, pre, usd, post, None, sys_note, pos is not None, None))
            continue
        seen += 1
        f = post / pre
        rows.append((f, sym, pre, usd, post, bucket(f), sys_note, pos is not None, dec["reason"]))

    rows.sort(key=lambda r: -(r[0] if r[0] is not None else -1.0))
    for i, (f, sym, pre, usd, post, bkt, sys_note, _, _reason) in enumerate(rows, 1):
        fs = f"{f:6.2f}" if f is not None else "     -"
        bs = (bkt or "no-snap")[:9]
        posts = f"{post:10,.0f}" if post else "         -"
        print(f"{i:>2d} {sym[:12]:12s} {pre:10,.0f} ${usd/1e6:>9.2f}M {posts} {fs:>6s} {bs:>9s}  {sys_note}")

    print(f"\nsummary: {len(rows)} target | {seen} with 90s snapshot (fold computed) | {missed} never seen by us")

    counts = {}
    species_b = 0
    for f, sym, pre, usd, post, bkt, sys_note, has_pos, reason in rows:
        if bkt is None:
            continue
        counts[bkt] = counts.get(bkt, 0) + 1
        if reason and str(reason or "").startswith("mcap_ceiling"):
            species_b += 1

    print(f"\nbuckets (of {seen} with snapshots):")
    for b in ("dusted", "faded", "flat", "continued", "ran away"):
        n = counts.get(b, 0)
        pct = 100.0 * n / seen if seen else 0.0
        print(f"  {b:>10s}: {n:>3d}  ({pct:>4.1f}%)")
    cont = counts.get("continued", 0) + counts.get("ran away", 0)
    dust = counts.get("dusted", 0)
    print(f"  {'continued+ran':>10s}: {cont:>3d}  vs dusted {dust}   -> "
          f"{'lane ALIVE' if cont > 0 else 'lane dead (verify near-miss slice first)'}")
    print(f"  species-B (ceiling rejects) in target list: {species_b}/{seen}")
    c.close()


if __name__ == "__main__":
    main()