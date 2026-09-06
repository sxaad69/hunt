from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import httpx
from loguru import logger

from hunt.config import get_settings
from hunt.universe.ohlcv import OhlcvFetcher
from hunt.utils.concurrency import GlobalRateBucket, worker_pool_sem

DB_PATH = "hunt/data/hunt.sqlite3"
API = "https://frontend-api-v3.pump.fun/coins"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pump_grads (
    mint TEXT PRIMARY KEY,
    symbol TEXT,
    created_ts INTEGER,
    twitter TEXT,
    telegram TEXT,
    website TEXT,
    market_cap REAL,
    candles_fetched INTEGER DEFAULT 0,
    first_candle_ts INTEGER,
    collected_at INTEGER
);
"""


class PumpGradCollector:
    def __init__(self) -> None:
        s = get_settings()
        self.s = s
        self.http = httpx.AsyncClient()
        self.bucket = GlobalRateBucket(0.8, 4)
        self.sem = worker_pool_sem(2)
        from hunt.gmgn.client import GmgnClient

        self.client = GmgnClient(s.gmgn_api_key)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH)
        conn.executescript(SCHEMA)
        return conn

    async def page_graduated(self, cutoff_ts_ms: int) -> list[dict]:
        rows: list[dict] = []
        offset = 0
        while True:
            await self.bucket.acquire()
            r = await self.http.get(
                API,
                params={
                    "offset": offset,
                    "limit": 70,
                    "sort": "created_timestamp",
                    "order": "DESC",
                    "complete": "true",
                },
                headers={"accept": "application/json"},
                timeout=20,
            )
            if r.status_code != 200:
                logger.warning("grad page {} failed {}", offset, r.status_code)
                break
            batch = r.json() or []
            if not batch:
                break
            rows.extend(batch)
            oldest = min(int(b.get("created_timestamp") or 0) for b in batch)
            if oldest < cutoff_ts_ms or len(batch) < 70:
                break
            offset += 70
        return [b for b in rows if int(b.get("created_timestamp") or 0) >= cutoff_ts_ms]

    async def stage1_collect_list(self) -> int:
        conn = self._conn()
        cutoff = (time.time() - self.s.backtest_days * 86400) * 1000
        grads = await self.page_graduated(cutoff)
        for g in grads:
            conn.execute(
                """INSERT OR IGNORE INTO pump_grads
                   (mint,symbol,created_ts,twitter,telegram,website,market_cap,collected_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    g.get("mint"), g.get("symbol"),
                    int(g.get("created_timestamp") or 0) // 1000,
                    g.get("twitter"), g.get("telegram"), g.get("website"),
                    float(g.get("market_cap") or 0), int(time.time()),
                ),
            )
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM pump_grads").fetchone()[0]
        days = conn.execute(
            "SELECT COUNT(DISTINCT date(created_ts,'unixepoch')), MIN(created_ts), MAX(created_ts) FROM pump_grads"
        ).fetchone()
        conn.close()
        logger.info(
            "stage1 done: +{} this run | total {} grads across {} days ({} → {})",
            len(grads), n, days[0],
            time.strftime("%m-%d", time.gmtime(days[1])),
            time.strftime("%m-%d", time.gmtime(days[2])),
        )
        return n

    async def fetch_candles_for(self, mint: str, created_ts: int) -> tuple[int, int]:
        candles = await self.client.klines(
            mint, resolution="1m", from_ts=created_ts - 1200,
            to_ts=created_ts + 6 * 3600,
        )
        if not candles:
            return created_ts, 0
        first = min(c["time"] for c in candles)
        p = Path(self.s.data_dir) / "cache" / "klines_1m"
        p.mkdir(parents=True, exist_ok=True)
        (p / f"{mint}.json").write_text(json.dumps({
            "fetched_at": int(time.time()), "candles": candles,
        }))
        return first, len(candles)

    async def stage2_fetch_candles(self, limit: int = 4000) -> int:
        conn = self._conn()
        todo = conn.execute(
            "SELECT mint, created_ts FROM pump_grads WHERE candles_fetched=0 LIMIT ?",
            (limit,),
        ).fetchall()
        logger.info("stage2: fetching 1m klines for {} tokens", len(todo))
        done = 0

        async def work(mint: str, created_ts: int):
            nonlocal done
            async with self.sem:
                try:
                    first, count = await self.fetch_candles_for(mint, created_ts)
                except Exception as e:
                    logger.debug("kline fail {}: {}", mint[:8], e)
                    return
            if count:
                conn.execute(
                    "UPDATE pump_grads SET candles_fetched=1, first_candle_ts=? WHERE mint=?",
                    (first, mint),
                )
                done += 1

        await asyncio.gather(*(work(m, ts) for m, ts in todo), return_exceptions=True)
        conn.commit()
        total = conn.execute(
            "SELECT COUNT(*) FROM pump_grads WHERE candles_fetched=1"
        ).fetchone()[0]
        conn.close()
        logger.info("stage2 batch done: {} fetched now | {} total with candles", done, total)
        return total

    async def run_forever(self) -> None:
        while True:
            try:
                await self.stage1_collect_list()
                await self.stage2_fetch_candles()
            except Exception as e:
                logger.exception("collector loop error: {}", e)
            await asyncio.sleep(1800)


class _KvShim:
    async def kv_get_int(self, *a) -> int:
        return 0

    async def kv_bump_daily(self, *a, **k) -> int:
        return 1


if __name__ == "__main__":
    asyncio.run(PumpGradCollector().run_forever())
