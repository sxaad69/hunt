#!/usr/bin/env python3
"""All SL closes in the window vs smart-money: our tracked list vs GMGN +sm/+kol tags.

Usage: .venv/bin/python audit/sl_smart.py [hours=24]
"""
from __future__ import annotations

import re
import sqlite3
import sys
import time

HOURS = int(sys.argv[1]) if len(sys.argv) > 1 else 24
DB = "hunt/data/hunt.sqlite3"


def main():
    since = int(time.time()) - HOURS * 3600
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        """SELECT p.symbol, p.mint, p.opened_ts, p.closed_ts, p.pnl_sol,
                  d.reason, d.market_cap,
                  (SELECT COUNT(*) FROM wallets w
                   JOIN wallet_token_edges e ON e.wallet=w.address
                   WHERE w.status='tracked' AND w.source='gmgn_smartmoney'
                     AND e.mint=p.mint) AS tracked,
                  (SELECT COUNT(*) FROM wallet_token_edges e
                   WHERE e.mint=p.mint AND e.source='gmgn_smartmoney') AS sm_edges
           FROM positions p
           LEFT JOIN paper_decisions d ON d.mint=p.mint
           WHERE p.exit_reason='stop_loss' AND p.closed_ts>=?
           ORDER BY p.closed_ts DESC""",
        (since,),
    ).fetchall()

    boost = sm_tag = kol_tag = tracked_n = none = 0
    boost_rows = []
    tracked_rows = []
    for sym, mint, op, cl, pnl, reason, mcap, tr, edges in rows:
        reason = reason or ""
        is_boost = "smart_boost" in reason or "+smart_" in reason
        is_sm = bool(re.search(r"\+sm\d+", reason))
        is_kol = bool(re.search(r"\+kol\d+", reason))
        if is_boost:
            boost += 1
            boost_rows.append((sym, mint, reason, mcap, tr, edges, cl - op))
        elif tr:
            tracked_n += 1
            tracked_rows.append((sym, mint, reason, mcap, tr, edges, cl - op))
        if is_sm:
            sm_tag += 1
        if is_kol:
            kol_tag += 1
        if not is_boost and not tr and not is_sm and not is_kol:
            none += 1

    print(f"SL smart-money audit: {len(rows)} stop-losses in {HOURS}h")
    print(f"  smart_boost (our tracked flipped entry): {boost}")
    print(f"  tracked wallet on mint but no boost:     {tracked_n}")
    print(f"  GMGN +smN tag on ACCEPT:                 {sm_tag}")
    print(f"  GMGN +kolN tag on ACCEPT:                {kol_tag}")
    print(f"  no smart signal at all:                  {none}")
    print()
    if boost_rows:
        print("smart_boost SLs:")
        print(f"  {'token':12s} {'entry':>8s} {'held':>5s} {'trk':>3s} {'edg':>3s}  reason")
        for sym, mint, reason, mcap, tr, edges, held in boost_rows:
            hs = f"{held}s" if held < 120 else f"{held/60:.0f}m"
            print(f"  {(sym or '?'):12s} {float(mcap or 0):8.0f} {hs:>5s} {tr:3d} {edges:3d}  {reason}")
    if tracked_rows:
        print("\ntracked-on-mint (not boost) SLs:")
        print(f"  {'token':12s} {'entry':>8s} {'held':>5s} {'trk':>3s} {'edg':>3s}  reason")
        for sym, mint, reason, mcap, tr, edges, held in tracked_rows:
            hs = f"{held}s" if held < 120 else f"{held/60:.0f}m"
            print(f"  {(sym or '?'):12s} {float(mcap or 0):8.0f} {hs:>5s} {tr:3d} {edges:3d}  {reason}")
    conn.close()


if __name__ == "__main__":
    main()
