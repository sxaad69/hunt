#!/usr/bin/env python3
import asyncio, sqlite3
import httpx
from hunt.scout.dexscreener import DexScreener

DB="hunt/data/hunt.sqlite3"

async def main():
    conn=sqlite3.connect(DB)
    rows=conn.execute("SELECT mint, symbol, market_cap, created_ts FROM paper_decisions WHERE decision='REJECT' ORDER BY decided_at DESC LIMIT 163").fetchall()
    print(f"checking {len(rows)} REJECTED")
    ds=DexScreener(httpx.AsyncClient(timeout=15))
    # batch 30
    results=[]
    for i in range(0, len(rows), 30):
        batch=rows[i:i+30]
        mints=[r[0] for r in batch]
        try:
            pairs=await ds.pairs_for_mints(mints)
        except Exception as e:
            print(f"batch {i//30} fail {e}")
            pairs={}
        for mint, symbol, init_mc, ts in batch:
            ht=pairs.get(mint)
            cur_price=ht.price_usd if ht and ht.price_usd else 0
            cur_mc=getattr(ht, "market_cap", 0) or 0
            cur_liq=getattr(ht, "liquidity_usd", 0) or 0
            gain=None
            if init_mc and cur_mc:
                try: gain=(cur_mc/init_mc-1)*100
                except: gain=None
            results.append((mint, symbol, init_mc, cur_mc, cur_price, cur_liq, gain))
        await asyncio.sleep(0.5)
        print(f"  batch {i//30+1}/{(len(rows)+29)//30} done")
    # sort by gain
    results_sorted=sorted([r for r in results if r[6] is not None], key=lambda x: x[6], reverse=True)
    print("\n=== TOP REJECTED by mcap gain (would we have missed moonshot?) ===")
    for mint, symbol, init_mc, cur_mc, cur_price, cur_liq, gain in results_sorted[:20]:
        print(f"{symbol:12} {mint[:8]} init ${init_mc:.0f} -> cur ${cur_mc:.0f} ({gain:+.1f}%) price ${cur_price:.6g} liq ${cur_liq:.0f}")
    print("\n=== BOTTOM REJECTED (dead) ===")
    for mint, symbol, init_mc, cur_mc, cur_price, cur_liq, gain in results_sorted[-10:]:
        print(f"{symbol:12} {mint[:8]} init ${init_mc:.0f} -> cur ${cur_mc:.0f} ({gain:+.1f}%)")
    # also count how many would have been winners (+100% = moonshot, +30% = TP)
    winners_100=sum(1 for r in results_sorted if r[6] is not None and r[6]>=100)
    winners_30=sum(1 for r in results_sorted if r[6] is not None and r[6]>=30)
    print(f"\nREJECT winners: +100% {winners_100}/{len(results_sorted)} ({winners_100/len(results_sorted)*100:.1f}%), +30% {winners_30}/{len(results_sorted)} ({winners_30/len(results_sorted)*100:.1f}%)")
    # compare to ACCEPT closed/open gain for reference
    # also check how many REJECT now dead (no pair)
    no_pair=sum(1 for r in results if r[3]==0)
    print(f"no pair (dead/rug) {no_pair}/{len(results)}")

if __name__=="__main__":
    asyncio.run(main())
