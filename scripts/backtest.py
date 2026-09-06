import argparse
import asyncio
import sqlite3
import sys
import time

import httpx

from hunt.config import get_settings
from hunt.score.metrics import build_closed_trades
from hunt.score.replay import ReplayEngine
from hunt.utils.swaps import WalletTradeLike


class FakeDb:
    async def kv_get_int(self, *a):
        return 0

    async def kv_bump_daily(self, *a):
        return 1


def fmt_age(ts: int) -> str:
    d = time.time() - ts
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


async def fetch_symbols(client, mints):
    out = {}
    for i in range(0, len(mints), 30):
        chunk = mints[i : i + 30]
        try:
            r = await client.get(
                "https://api.dexscreener.com/tokens/v1/solana/" + ",".join(chunk), timeout=15
            )
            if r.status_code == 200:
                for pair in r.json() or []:
                    base = pair.get("baseToken") or {}
                    if base.get("address") and base.get("symbol"):
                        out.setdefault(base["address"], base["symbol"])
        except Exception:
            pass
    return out


async def main():
    ap = argparse.ArgumentParser(description="backtest candidate wallets' recent positions")
    ap.add_argument("--wallets", type=int, default=6)
    ap.add_argument("--max-txs", type=int, default=120)
    ap.add_argument("--last", type=int, default=5, help="positions per wallet to show")
    args = ap.parse_args()

    s = get_settings()
    db = sqlite3.connect("hunt/data/hunt.sqlite3")
    wallets = [
        r[0] for r in db.execute(
            "SELECT address FROM wallets WHERE status='candidate' ORDER BY added_at LIMIT ?",
            (args.wallets,),
        )
    ]
    if not wallets:
        print("no candidates in db yet")
        return 1

    http = httpx.AsyncClient()
    engine = ReplayEngine(http, FakeDb())
    engine.limiter.rate_per_sec = 6.0
    engine.limiter.capacity = 12
    grand_pnl = 0.0
    grand_trades = 0
    grand_wins = 0
    real_traders = 0
    all_mints = set()

    for w in wallets:
        tag = f"{w[:8]}…{w[-4:]}"
        trades = await engine.fetch_history(w, max_txs=args.max_txs)
        if not trades:
            print(f"\n=== {tag}: MEV/bot pattern — unpaired WSOL flows, no copyable swaps ❌")
            continue
        closed, open_bags = build_closed_trades(trades)
        if not closed and not open_bags:
            print(f"\n=== {tag}: {len(trades)} swaps but no closable cycles — irregular ❌")
            continue
        if not closed:
            print(f"\n=== {tag}: {len(open_bags)} open bags, no closed positions yet ⏳")
            continue
        real_traders += 1

        closed.sort(key=lambda c: c.exit_ts)
        recent = closed[-args.last :]
        wins = sum(1 for c in recent if c.pnl > 0)
        pnl_sum = sum(c.pnl for c in recent)
        grand_pnl += sum(c.pnl for c in closed)
        grand_trades += len(closed)
        grand_wins += sum(1 for c in closed if c.pnl > 0)
        for c in closed:
            all_mints.add(c.mint)

        print(f"\n=== {tag} | {len(trades)} swaps → {len(closed)} closed positions "
              f"| open bags: {len(open_bags)}")
        print(f"    last {len(recent)} positions: {wins}/{len(recent)} wins | net {pnl_sum:+.3f} SOL")
        for c in reversed(recent):
            hold = f"{c.hold_s // 60}m" if c.hold_s < 86400 else f"{c.hold_s // 3600}h"
            mark = "🟢" if c.pnl > 0 else "🔴"
            print(f"    {mark} {fmt_age(c.exit_ts):>7} {c.mint[:8]}…  "
                  f"in {c.sol_in:>7.3f} → out {c.sol_out:>8.3f} SOL  "
                  f"pnl {c.pnl:>+8.3f}  held {hold}")

    syms = await fetch_symbols(http, list(all_mints))
    print("\ntokens seen:", ", ".join(syms.get(m, m[:8]) for m in sorted(all_mints)[:20]))
    wr = (grand_wins / grand_trades * 100) if grand_trades else 0
    print(f"\nTOTAL: {grand_trades} closed positions across {real_traders}/{len(wallets)} real traders, "
          f"win rate {wr:.0f}%, net {grand_pnl:+.2f} SOL")
    await http.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
