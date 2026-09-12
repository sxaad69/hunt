#!/usr/bin/env python3
"""Top N pump.fun gainers launched today only (ranked by change since birth),
with a **rejection-reason breakdown** so we can see WHY species-A movers
(the ones that ride up through our entry band) got rejected.

pump.fun coins have no "24h candle" — change = current mcap / birth mcap
(~$4.5K / 28 SOL). Rank by that multiple; cross-reference our decisions table;
then summarize the reject reasons into strategy buckets:

  - dust floor       judged in-band but mcap <50 SOL at decision
  - ceiling/species-B judged already above 3000 SOL (unplayable, NOT a miss)
  - bundled/snipers  top10-heavy or sniper bundles (quality veto)
  - no socials / low survival / dead hour   metadata & model gates
  - stale/no demand  no trade inside 180s at decision
  - risk             SolanaTracker risk gate
  - dev reputation   serial ruggers
  - accepted         passed every gate (traded or untraded)

Usage: python audit/top_gainers.py [N] [hours] [min_change_x]
   N            top N to print (default 30)
   hours        how far back "launched today" reaches (default 24)
   min_change_x change-floor; only coins >= this multiple qualify (default 10)
"""
import sqlite3
import sys
import time

import httpx

N = int(sys.argv[1]) if len(sys.argv) > 1 else 30
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


def bucket(reason: str, decision: str, has_pos: bool) -> tuple[str, bool]:
    """Return (bucket, is_speciesB). is_speciesB = ceiling reject (unplayable)."""
    r = (reason or "").lower()
    if has_pos or decision == "ACCEPT":
        return ("accepted/traded" if has_pos else "accepted (untraded)"), False
    if r.startswith("mcap_ceiling"):
        return "ceiling (species-B, >3000 SOL at decision)", True
    if r.startswith("dust_mcap"):
        return "dust floor (<50 SOL)", False
    if r.startswith("sniper_bundle") or r.startswith("top10"):
        return "bundled / snipers", False
    if r.startswith("no_socials"):
        return "no socials", False
    if r.startswith("low_p"):
        return "survival model low", False
    if r.startswith("stale"):
        return "stale / no demand", False
    if r.startswith("risk"):
        return "risk gate", False
    if r.startswith("dead_hour"):
        return "dead hour", False
    if r.startswith("serial") or r.startswith("dev"):
        return "dev reputation", False
    return f"other ({reason})", False


def main():
    now_ms = time.time() * 1000
    coins = {}
    min_usd = BIRTH_USD * MIN_X
    for complete in ("true", "false"):
            for off in range(0, 5001, 100):
            try:
                batch = fetch("https://frontend-api-v3.pump.fun/coins",
                              params={"offset": off, "limit": 100, "sort": "market_cap",
                                      "order": "DESC", "complete": complete})
            except Exception:
                continue
            batch = batch or []
            if not batch or max(float(x.get("usd_market_cap") or 0) for x in batch) < min_usd:
                break  # sorted by mcap desc — anything deeper can't reach the floor
            for x in batch:
                m = x.get("mint")
                if not m or not str(m).endswith("pump"):
                    continue
                usd = float(x.get("usd_market_cap") or 0)
                created = x.get("created_timestamp") or 0
                age_h = (now_ms - created) / 3.6e6
                if 0 < age_h <= HOURS and usd >= min_usd:
                    coins[m] = (x.get("symbol") or "?", m, usd, age_h, now_ms, created)

    ranked = sorted(coins.values(), key=lambda t: - (t[2] / BIRTH_USD))
    ranked = ranked[:N]

    print(f"top {len(ranked)} pump.fun gainers launched in the last {HOURS}h (>= {MIN_X:.0f}x), ranked by change since birth\n")
    print(f"{'#':>2s} {'token':12s} {'mcap':>11s} {'x-birth':>7s} {'age':>5s}  our-system")
    caught = seen = unseen = 0
    details = []
    for i, (sym, mint, usd, age_h, _, created) in enumerate(ranked, 1):
        mult = usd / BIRTH_USD
        pos = c.execute("SELECT status, ROUND(pnl_sol,4), exit_reason FROM positions WHERE mint=?",
                        (mint,)).fetchone()
        dec = c.execute("SELECT decision, reason, ROUND(market_cap,0) FROM paper_decisions WHERE mint=?",
                        (mint,)).fetchone()
        if pos:
            caught += 1
            pnl = pos["pnl_sol"]
            pnl_s = f"{pnl:+.4f}" if pnl is not None else "open"
            what = f"POSITION {pos['status']} {pos['exit_reason'] or ''} {pnl_s} SOL"
        elif dec:
            seen += 1
            what = f"{dec['decision']}: {dec['reason']}"
        else:
            unseen += 1
            what = "never seen"
        details.append((dec, pos is not None))
        print(f"{i:>2d} {sym[:12]:12s} ${usd/1e6:>9.2f}M {mult:>6.0f}x {age_h:>4.1f}h  {what}")

    print(f"\nsummary: {len(ranked)} ranked | {caught} caught | {seen} seen+rejected | {unseen} never seen")

    total = len(ranked) or 1
    counts: dict = {}
    b_total = 0
    for dec, has_pos in details:
        if not dec:
            continue
        b, is_b = bucket(dec["reason"], dec["decision"], has_pos)
        counts[b] = counts.get(b, 0) + 1
        b_total += is_b

    print("\nrejection reasons (count of the movers above, grouped by our gate):")
    print(f"{'reason bucket':38s} {'count':>6s} {'share':>7s}")
    for b, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"{b:38s} {n:>6d} {100.0*n/total:>6.1f}%")
    print(f"\nspecies B (ceiling, unreachable): {b_total}/{len(ranked)}  "
          f"| species A pool (rejected in-band): {len(ranked)-b_total-caught-unseen}/{len(ranked)}")
    c.close()


if __name__ == "__main__":
    main()