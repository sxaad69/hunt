#!/usr/bin/env python3
"""Top runners of the last 24h — with what our system did about each.

Ranks by realized multiple (current usd_mcap / birth mcap ~$4.5K) and
cross-references our decisions table: position taken, or reject reason.
Usage: python scripts/top_runners.py [hours]
"""
import sqlite3
import sys
import time

import httpx

HOURS = int(sys.argv[1]) if len(sys.argv) > 1 else 24
BIRTH_USD = 4500.0
DB = "hunt/data/hunt.sqlite3"

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row


def fetch(url, **kw):
    with httpx.Client(timeout=15, headers={"accept": "application/json",
                                           "user-agent": "Mozilla/5.0"}) as cl:
        return cl.get(url, **kw).json()


def main():
    now_ms = time.time() * 1000
    seen, coins = set(), []
    for complete in ("true", "false"):
        for off in (0, 100):
            try:
                batch = fetch("https://frontend-api-v3.pump.fun/coins",
                              params={"offset": off, "limit": 100, "sort": "market_cap",
                                      "order": "DESC", "complete": complete})
            except Exception:
                continue
            for x in batch or []:
                m = x.get("mint")
                if not m or m in seen or not str(m).endswith("pump"):
                    continue
                seen.add(m)
                usd = float(x.get("usd_market_cap") or 0)
                created = x.get("created_timestamp") or 0
                age_h = (now_ms - created) / 3.6e6
                if usd >= BIRTH_USD * 10 and age_h <= HOURS:   # 10x+ today
                    coins.append((x.get("symbol") or "?", m, usd, age_h))
    coins.sort(key=lambda t: -t[2])

    print(f"top runners (>=10x) in the last {HOURS}h: {len(coins)}\n")
    print(f"{'token':12s} {'mcap':>12s} {'mult':>7s} {'age':>5s}  our-system")
    caught = rejected = unseen = 0
    for sym, mint, usd, age_h in coins:
        mult = usd / BIRTH_USD
        pos = c.execute("SELECT status, ROUND(pnl_sol,4), exit_reason FROM positions WHERE mint=?",
                        (mint,)).fetchone()
        dec = c.execute("SELECT decision, reason, ROUND(market_cap,0) FROM paper_decisions WHERE mint=?",
                        (mint,)).fetchone()
        if pos:
            caught += 1
            what = f"POSITION {pos['status']} {pos['exit_reason'] or ''} {pos[1]:+.4f} SOL"
        elif dec:
            rejected += 1
            what = f"{dec['decision']}: {dec['reason']}"
        else:
            unseen += 1
            what = "never seen"
        print(f"{sym[:12]:12s} ${usd/1e6:>10.2f}M {mult:>6.0f}x {age_h:>4.1f}h  {what}")
    print(f"\nsummary: {len(coins)} runners | {caught} caught | "
          f"{rejected} seen+rejected | {unseen} never seen")
    c.close()


if __name__ == "__main__":
    main()
