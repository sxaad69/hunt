"""paper_stops_loop must survive startup and shut down cleanly.

An UnboundLocalError (a late `from hunt.config import get_settings` shadowing
the early use) once killed the whole loop on its first line: no uncovered
polling, no kill-file, no loss-cap, no exits — for an entire live session,
silently (shutdown swallowed the exception). This test runs the real loop
briefly against a scratch DB: any startup crash fails it.
"""
import asyncio
import os
import sqlite3
import tempfile

import hunt.paper.run as runmod


def test_stops_loop_startup_and_shutdown():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    c = sqlite3.connect(path)
    c.execute(
        "CREATE TABLE positions (id INTEGER PRIMARY KEY, opened_ts INT, mint TEXT,"
        " symbol TEXT, mode TEXT, size_sol REAL, tokens REAL, entry_price_usd REAL,"
        " peak_price_usd REAL, tp_tier INT, realized_sol REAL, status TEXT,"
        " closed_ts INT, exit_reason TEXT, exit_sol REAL, pnl_sol REAL, decimals INT)")
    c.commit()
    c.close()
    old_db = runmod.DB_PATH
    runmod.DB_PATH = path
    runmod._px_seen.clear()
    runmod._blind_warned.clear()
    runmod._jup_px_cache.clear()
    try:
        async def main():
            ev = asyncio.Event()
            t = asyncio.create_task(runmod.paper_stops_loop(ev))
            await asyncio.sleep(2.0)
            ev.set()
            await asyncio.wait_for(t, timeout=30)

        asyncio.run(main())  # raises if the loop dies on startup
    finally:
        runmod.DB_PATH = old_db
        os.remove(path)
