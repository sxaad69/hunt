#!/usr/bin/env python3
"""Survival-agnostic species-B SL-death measurement.

Companion to specb_mcap_audit.py. That script samples pump.fun's RANKED
GAINERS — survivor-biased: dusted coins fall off the ranking, so the SL-death
class is invisible to it (it sizes the upside). THIS script closes that gap by
walking the OTHER direction: population = every species-B candidate WE saw
(decisions DB), including the dead ones, and measures what the ladder would
have done if we'd entered all of them.

  population = DISTINCT mints with 90s snapshot mcap >= MIN_MCAP in last HOURS,
               restricted to coins that clear the NON-ceiling gates
               (top10<=75 AND snipers<2; missing intel = pass, e.g. ZDOG path)
  fold       = current SOL mcap / 90s SOL mcap   (coin GONE/404 -> fold 0 = dead)
  buckets    = <0.2 dusted(SL-death) · 0.2-0.8 faded · 0.8-1.2 flat · >1.2
               continued · >=5 ran away

  SL-death rate (fold<0.2) is the number AGENTS.md flagged as MUST-next before
  trusting the Sep-8 species-B reopen. Same caveats as the specb script: current
  mcap is a lower bound (peak-then-dump invisible), so "dusted" is a floor on
  the true death rate and a continued coin may still have SL'd intraday.

Usage: sudo -u hunt .venv/bin/python audit/specb_sl_death.py [hours=48] [min_mcap=3000]
"""
import sqlite3
import sys
import time

import httpx

HOURS = int(sys.argv[1]) if len(sys.argv) > 1 else 48
MIN_MCAP = float(sys.argv[2]) if len(sys.argv) > 2 else 3000.0
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
    cut = time.time() - HOURS * 3600
    print(f"species-B SL-death audit: {HOURS}h @ >= {MIN_MCAP:.0f} SOL, SOL=${sol:,.2f}")

    # population: earliest species-B observation per mint + its intel
    rows = c.execute("""
        SELECT mint, symbol, MIN(decided_at) AS decided_at, market_cap,
               top10, snipers, holders, reason
        FROM paper_decisions
        WHERE market_cap >= ? AND decided_at >= ?
        GROUP BY mint ORDER BY decided_at
    """, (MIN_MCAP, cut)).fetchall()
    print(f"distinct species-B mints seen: {len(rows)}")

    pop = []
    nointel = vetoed = 0
    for r in rows:
        t10, sn = r["top10"], r["snipers"]
        if t10 is None or sn is None:
            nointel += 1
            pop.append(r)          # ZDOG path: unmeasured -> enters
        elif float(t10) > 75 or float(sn or 0) >= 2:
            vetoed += 1            # sniper/top10 gates still keep this out
        else:
            pop.append(r)
    print(f"would-be-entered after non-ceiling gates: {len(pop)} "
          f"(+{nointel} no-intel pass, -{vetoed} sniper/top10 vetoed)")

    counts = {}
    gone = 0
    no_snapshot = 0
    folds = []
    cl = httpx.Client(timeout=15, headers={"accept": "application/json",
                                           "user-agent": "Mozilla/5.0"})
    for i, r in enumerate(pop, 1):
        try:
            j = cl.get(f"https://frontend-api-v3.pump.fun/coins/{r['mint']}").json()
        except Exception:
            j = None
        usd = 0.0 if not j else float((j or {}).get("usd_market_cap") or 0)
        now_sol = usd / sol if usd > 0 else 0.0
        if usd <= 0:
            gone += 1             # 404 / GONE / unparseable = dead
        pre = float(r["market_cap"])
        if pre <= 0:
            no_snapshot += 1
            bkt = None
            f = None
        else:
            f = now_sol / pre
            bkt = bucket(f) if f is not None else None
            folds.append(f)
        counts[bkt] = counts.get(bkt, 0) + 1
        if i % 50 == 0:
            print(f"  ...{i}/{len(pop)}  gone={gone}  median-fold="
                  f"{(sorted(folds)[len(folds)//2] if folds else 0):.3f}")
        time.sleep(0.03)

    n = len(pop) - no_snapshot
    buckets = ("dusted", "faded", "flat", "continued", "ran away")
    print(f"\nfold buckets (of {n} with snapshots; missing/GONE={gone} counted as dusted):")
    for b in buckets:
        cnt = counts.get(b, 0) + (gone if b == "dusted" else 0)
        pct = 100.0 * cnt / (n + gone) if (n + gone) else 0.0
        print(f"  {b:>10s}: {cnt:>4d}  ({pct:>4.1f}%)")
    dust = counts.get("dusted", 0) + gone
    faded = counts.get("faded", 0)
    cont = counts.get("continued", 0) + counts.get("ran away", 0)
    total = n + gone
    print(f"\n  SL-death (dusted    <= {total or 0}):  {100.0*dust/max(total,1):>5.1f}%  <-- the AGENTS number")
    print(f"  losing entries (dusted+faded):          {100.0*(dust+faded)/max(total,1):>5.1f}%")
    print(f"  continued/ran (fold>=1.2):              {100.0*cont/max(total,1):>5.1f}%")
    if folds:
        folds.sort()
        print(f"  median fold: {folds[len(folds)//2]:.3f}   "
              f"(p25={folds[len(folds)//4]:.3f}, p75={folds[3*len(folds)//4]:.3f}, "
              f"max={folds[-1]:.2f})")
    print(f"  caveat: current mcap is a lower bound; peak-then-dump and "
          f"intraday-SL-before-recovery invisible")
    c.close()


if __name__ == "__main__":
    main()