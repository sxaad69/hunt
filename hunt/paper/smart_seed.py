"""Plan B: seed local wallets+edges from GMGN smart-money buys.

GMGN is the seeder only (every 8 min). ACCEPT top-traders are never promoted.
Wallets with 2+ distinct *pump mints from gmgn_smartmoney become status=tracked.
check_smart_buy reads that table — no live GMGN on the judge path.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

from loguru import logger

DB = "hunt/data/hunt.sqlite3"
SEED_INTERVAL_S = 480.0
MIN_BUY_USD = 40.0


def tracked_count() -> int:
    try:
        conn = sqlite3.connect(DB, timeout=10)
        n = conn.execute(
            "SELECT COUNT(*) FROM wallets WHERE status='tracked' AND source='gmgn_smartmoney'"
        ).fetchone()[0]
        conn.close()
        return int(n or 0)
    except Exception:
        return 0


def _upsert_edge(wallet: str, mint: str, source: str, rank: int) -> None:
    today = time.strftime("%Y-%m-%d", time.gmtime())
    conn = sqlite3.connect(DB, timeout=10)
    conn.execute(
        "INSERT OR IGNORE INTO wallets(address, source, status, first_token, added_at) VALUES(?,?,?,?,?)",
        (wallet, source, "candidate", mint, int(time.time())),
    )
    conn.execute(
        "INSERT OR IGNORE INTO wallet_token_edges(wallet,mint,day,source,rank_in_token) VALUES(?,?,?,?,?)",
        (wallet, mint, today, source, rank),
    )
    conn.commit()
    conn.close()


def _promote_multi() -> int:
    conn = sqlite3.connect(DB, timeout=10)
    conn.execute(
        "UPDATE wallets SET status='candidate' WHERE source!='gmgn_smartmoney' AND status='tracked'"
    )
    rows = conn.execute(
        """SELECT e.wallet, COUNT(DISTINCT e.mint) n
           FROM wallet_token_edges e
           JOIN wallets w ON w.address=e.wallet
           WHERE e.source='gmgn_smartmoney' AND w.source='gmgn_smartmoney'
           GROUP BY e.wallet HAVING n >= 2"""
    ).fetchall()
    n = 0
    for w, _c in rows:
        cur = conn.execute(
            "UPDATE wallets SET status='tracked' WHERE address=? AND source='gmgn_smartmoney' AND status!='tracked'",
            (w,),
        )
        n += cur.rowcount or 0
    conn.commit()
    conn.close()
    return n


async def seed_once() -> None:
    from hunt.config import get_settings
    s = get_settings()
    try:
        from hunt.gmgn.client import GmgnClient
        client = GmgnClient(s.gmgn_api_key)
        added = 0
        trades = await client.smart_money_trades(limit=80)
        for t in trades:
            mint = t.mint or ""
            if t.side.lower() != "buy" or not t.wallet or not mint.endswith("pump"):
                continue
            if t.amount_usd < MIN_BUY_USD:
                continue
            _upsert_edge(t.wallet, mint, "gmgn_smartmoney", 0)
            added += 1
        promoted = _promote_multi()
        logger.info("smart-seed edges+={} promoted={} tracked={}",
                    added, promoted, tracked_count())
    except Exception as e:
        logger.info("smart-seed idle — {} (tracked={})", e, tracked_count())


async def smart_seed_loop(stop_event: asyncio.Event, interval_s: float = SEED_INTERVAL_S) -> None:
    await seed_once()
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            return
        await seed_once()
