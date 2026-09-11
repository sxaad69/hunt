"""Plan B: fill wallets+edges so check_smart_buy can fire.

Uses GMGN top traders on recent ACCEPT mints when a key exists.
Wallets with 2+ distinct profitable tokens become status=tracked.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

from loguru import logger

DB = "hunt/data/hunt.sqlite3"


def tracked_count() -> int:
    try:
        conn = sqlite3.connect(DB, timeout=10)
        n = conn.execute("SELECT COUNT(*) FROM wallets WHERE status='tracked'").fetchone()[0]
        conn.close()
        return int(n or 0)
    except Exception:
        return 0


def _recent_accept_mints(limit: int = 15) -> list[str]:
    conn = sqlite3.connect(DB, timeout=10)
    rows = conn.execute(
        "SELECT mint FROM paper_decisions WHERE decision='ACCEPT' "
        "ORDER BY decided_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows if r and r[0]]


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
    rows = conn.execute(
        """SELECT wallet, COUNT(DISTINCT mint) n FROM wallet_token_edges
           GROUP BY wallet HAVING n >= 2"""
    ).fetchall()
    n = 0
    for w, _c in rows:
        conn.execute("UPDATE wallets SET status='tracked' WHERE address=? AND status!='tracked'", (w,))
        n += conn.total_changes
    conn.commit()
    conn.close()
    return n


async def seed_once() -> None:
    from hunt.config import get_settings
    s = get_settings()
    if not s.gmgn_api_key:
        logger.info("smart-seed idle — no GMGN key, tracked={}", tracked_count())
        return
    mints = _recent_accept_mints(12)
    if not mints:
        logger.info("smart-seed idle — no ACCEPT mints yet, tracked={}", tracked_count())
        return
    try:
        from hunt.gmgn.client import GmgnClient
        client = GmgnClient(s.gmgn_api_key)
        added = 0
        for mint in mints:
            traders = await client.token_top_traders(mint, limit=8)
            for i, t in enumerate(traders or []):
                addr = t.get("address") or t.get("wallet") or ""
                pnl = float(t.get("realized_profit") or 0)
                if not addr or pnl <= 0 or t.get("is_suspicious"):
                    continue
                _upsert_edge(addr, mint, "gmgn_toptrader", i + 1)
                added += 1
            await asyncio.sleep(1.0)
        promoted = _promote_multi()
        logger.info("smart-seed mints={} edges+={} promoted={} tracked={}",
                    len(mints), added, promoted, tracked_count())
    except Exception as e:
        logger.warning("smart-seed failed: {}", e)


async def smart_seed_loop(stop_event: asyncio.Event, interval_s: float = 900.0) -> None:
    await seed_once()
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            return
        await seed_once()
