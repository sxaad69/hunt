#!/usr/bin/env python3
from __future__ import annotations
import asyncio, json, sqlite3, time, statistics
from pathlib import Path
import httpx
from loguru import logger
from hunt.backtest.curve import TieredConfig, entry_price_ratio, simulate_tiered, dead_trade_usd
from hunt.utils.ratelimit import RateLimiter
from hunt.config import get_settings
GlobalRateBucket = RateLimiter

DB_PATH = "hunt/data/hunt.sqlite3"
KLINES_DIR = Path("hunt/data/cache/klines_1m")
VERIFY_OUT = Path("hunt/data/paper_verify.json")

async def fetch_candles_gmgn(mint: str, created_ts: int, bucket: GlobalRateBucket) -> list[dict]:
    from hunt.gmgn.client import GmgnClient
    s = get_settings()
    client = GmgnClient(s.gmgn_api_key)
    await bucket.acquire()
    try:
        candles = await client.klines(mint, resolution="1m", from_ts=created_ts-1200, to_ts=created_ts+6*3600)
        return candles or []
    except Exception as e:
        logger.debug("gmgn klines fail {}: {}", mint[:8], e)
        return []

def save_candles(mint: str, candles: list[dict]):
    KLINES_DIR.mkdir(parents=True, exist_ok=True)
    (KLINES_DIR / f"{mint}.json").write_text(json.dumps({"fetched_at": int(time.time()), "candles": candles}))

def load_candles(mint: str) -> list[dict]:
    p = KLINES_DIR / f"{mint}.json"
    if not p.exists(): return []
    try: return [c for c in json.loads(p.read_text()).get("candles",[]) if c.get("close")]
    except: return []

def first_open(candles):
    for c in candles:
        if c.get("open"): return float(c["open"])
    return None

async def verify(hours: int = 6):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT mint, symbol, created_ts, decision, reason FROM paper_decisions").fetchall()
    logger.info("paper_decisions: {} (ACCEPT {}, REJECT {})", len(rows),
                sum(1 for r in rows if r["decision"]=="ACCEPT"),
                sum(1 for r in rows if r["decision"]=="REJECT"))
    if not rows:
        print("no paper_decisions")
        return
    bucket = GlobalRateBucket(1.0, 2)
    base_ratio = entry_price_ratio(0.85)
    cfg = TieredConfig()
    dead_usd = dead_trade_usd(cfg)
    # fetch missing candles
    missing = []
    for r in rows:
        candles = load_candles(r["mint"])
        if not candles:
            missing.append(r)
    logger.info("missing candles: {}/{}", len(missing), len(rows))
    for i, r in enumerate(missing):
        candles = await fetch_candles_gmgn(r["mint"], r["created_ts"] or int(time.time()), bucket)
        if candles:
            save_candles(r["mint"], candles)
            logger.info("fetched {}/{} {} -> {} candles", i+1, len(missing), r["mint"][:8], len(candles))
        else:
            logger.info("no candles {}/{} {} (likely dead/rug)", i+1, len(missing), r["mint"][:8])
        await asyncio.sleep(0.2)
    # simulate
    results = []
    for r in rows:
        candles = load_candles(r["mint"])
        if not candles:
            results.append({"mint": r["mint"], "symbol": r["symbol"], "decision": r["decision"], "reason": r["reason"], "exit": "dead", "net_usd": dead_usd, "minutes": 0})
            continue
        o = first_open(candles)
        if not o or o<=0:
            results.append({"mint": r["mint"], "symbol": r["symbol"], "decision": r["decision"], "reason": r["reason"], "exit": "no_data", "net_usd": 0, "minutes": 0})
            continue
        entry = o * base_ratio
        out = simulate_tiered(candles, entry, cfg)
        out.update({"mint": r["mint"], "symbol": r["symbol"], "decision": r["decision"], "reason": r["reason"]})
        results.append(out)
    # aggregate
    def agg(sub):
        if not sub: return {}
        pnls=[x["net_usd"] for x in sub]
        wins=sum(1 for p in pnls if p>0)
        by_exit={}
        for x in sub: by_exit[x["exit"]]=by_exit.get(x["exit"],0)+1
        return {"n": len(pnls), "win_rate": round(wins/len(pnls)*100,1), "avg": round(statistics.mean(pnls),3), "median": round(statistics.median(pnls),3), "total": round(sum(pnls),2), "exits": by_exit}
    all_agg = agg(results)
    accept_agg = agg([x for x in results if x["decision"]=="ACCEPT"])
    reject_agg = agg([x for x in results if x["decision"]=="REJECT"])
    false_neg = [x for x in results if x["decision"]=="REJECT" and x["net_usd"]>0]
    false_pos = [x for x in results if x["decision"]=="ACCEPT" and x["net_usd"]<=0 and x["exit"]!="dead"]
    out_data = {
        "generated_at": int(time.time()),
        "hours": hours,
        "cfg": {"tiers": cfg.tiers, "trail": cfg.trail_pct, "sl": cfg.hard_sl, "position": cfg.position_usd},
        "overall": all_agg,
        "accept": accept_agg,
        "reject": reject_agg,
        "false_neg_reject_but_winner": sorted(false_neg, key=lambda x: -x["net_usd"])[:10],
        "false_pos_accept_but_loser": sorted(false_pos, key=lambda x: x["net_usd"])[:10],
        "all": results,
    }
    VERIFY_OUT.write_text(json.dumps(out_data, indent=2))
    print("\n=== PAPER VERIFY ===")
    print(f"overall: {all_agg}")
    print(f"ACCEPT: {accept_agg}")
    print(f"REJECT: {reject_agg}")
    print(f"false_neg REJECT-but-winner: {len(false_neg)}/{reject_agg.get('n',0)}")
    for x in out_data["false_neg_reject_but_winner"][:5]:
        print(f"  {x['symbol']} {x['mint'][:8]} {x['exit']} ${x['net_usd']:+.2f} reason={x['reason']}")
    print(f"false_pos ACCEPT-but-loser: {len(false_pos)}/{accept_agg.get('n',0)}")
    logger.info("written {}", VERIFY_OUT)
    return out_data

if __name__ == "__main__":
    import sys
    h = int(sys.argv[1]) if len(sys.argv)>1 else 6
    asyncio.run(verify(hours=h))
