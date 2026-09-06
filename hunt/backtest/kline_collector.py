from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import httpx
from loguru import logger

from hunt.config import get_settings
from hunt.gmgn.client import GmgnClient
from hunt.utils.concurrency import GlobalRateBucket, worker_pool_sem

DB_PATH = "hunt/data/hunt.sqlite3"
KLINES_DIR = Path("hunt/data/cache/klines_1m")
GT_BASE = "https://api.geckoterminal.com/api/v2"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA busy_timeout=20000")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(pump_grads)").fetchall()]
    if "source" not in cols:
        conn.execute("ALTER TABLE pump_grads ADD COLUMN source TEXT")
    conn.commit()
    return conn


def reconcile() -> int:
    if not KLINES_DIR.exists():
        return 0
    conn = _conn()
    fixed = 0
    for p in KLINES_DIR.glob("*.json"):
        mint = p.stem
        row = conn.execute(
            "SELECT candles_fetched FROM pump_grads WHERE mint=?", (mint,)
        ).fetchone()
        if row and row[0] == 1:
            continue
        try:
            meta = json.loads(p.read_text())
            candles = meta.get("candles") or []
            source = meta.get("source", "unknown")
            first = min((c["time"] for c in candles), default=None)
            if candles and first:
                conn.execute(
                    "UPDATE pump_grads SET candles_fetched=1, first_candle_ts=?, source=? WHERE mint=?",
                    (first, source, mint),
                )
                if not row:
                    conn.execute(
                        "INSERT INTO pump_grads(mint,candles_fetched,first_candle_ts,source,collected_at)"
                        " VALUES(?,1,?,?,?)",
                        (mint, first, source, int(time.time())),
                    )
                fixed += 1
        except Exception:
            continue
    conn.commit()
    conn.close()
    logger.info("reconcile: {} orphaned files claimed into db", fixed)
    return fixed


class KlineCollector:
    def __init__(self) -> None:
        s = get_settings()
        self.s = s
        self.http = httpx.AsyncClient(timeout=15)
        self.gt_bucket = GlobalRateBucket(rate_per_sec=0.4, burst=4)
        self.gt_sem = worker_pool_sem(3)
        self.gmgn = GmgnClient(s.gmgn_api_key)
        KLINES_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _cached(mint: str) -> bool:
        return (KLINES_DIR / f"{mint}.json").exists()

    @staticmethod
    def _save(mint: str, candles: list[dict], source: str, conn) -> int | None:
        if not candles:
            return None
        first = min(c["time"] for c in candles)
        (KLINES_DIR / f"{mint}.json").write_text(json.dumps({
            "fetched_at": int(time.time()), "source": source, "candles": candles,
        }))
        conn.execute(
            "UPDATE pump_grads SET candles_fetched=1, first_candle_ts=?, source=? WHERE mint=?",
            (first, source, mint),
        )
        return first

    async def gt_pool_address(self, mint: str) -> str | None:
        await self.gt_bucket.acquire()
        try:
            r = await self.http.get(
                f"{GT_BASE}/networks/solana/tokens/{mint}/pools",
                headers={"accept": "application/json"},
            )
            if r.status_code != 200:
                return None
            data = r.json().get("data") or []
            if not data:
                return None
            attrs = data[0].get("attributes") or {}
            return attrs.get("address")
        except Exception:
            return None

    async def gt_candles(self, pool: str) -> list[dict]:
        await self.gt_bucket.acquire()
        try:
            r = await self.http.get(
                f"{GT_BASE}/networks/solana/pools/{pool}/ohlcv/minute",
                params={"aggregate": 1, "limit": 1000, "currency": "usd"},
                headers={"accept": "application/json"},
            )
            if r.status_code != 200:
                if r.status_code == 429:
                    await asyncio.sleep(int(r.headers.get("retry-after", 30)))
                return []
            vals = ((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
            out = []
            for v in vals:
                if len(v) >= 5:
                    out.append({
                        "time": int(v[0]), "open": float(v[1]), "high": float(v[2]),
                        "low": float(v[3]), "close": float(v[4]),
                        "volume": float(v[5]) if len(v) > 5 else 0.0,
                    })
            return out
        except Exception:
            return []

    async def lane_gt(self, tokens: list[tuple[str, int]], conn) -> int:
        done = 0

        async def work(mint: str, created_ts: int):
            nonlocal done
            if self._cached(mint):
                return
            row = conn.execute("SELECT source FROM pump_grads WHERE mint=?", (mint,)).fetchone()
            if row and row[0] == "dead_recheck":
                return
            async with self.gt_sem:
                pool = await self.gt_pool_address(mint)
                if not pool:
                    conn.execute(
                        "UPDATE pump_grads SET candles_fetched=2, source='dead' WHERE mint=?",
                        (mint,),
                    )
                    conn.commit()
                    done += 1
                    return
                candles = await self.gt_candles(pool)
            if candles and len(candles) >= 3:
                if self._save(mint, candles, "geckoterminal", conn) is not None:
                    done += 1
                    conn.commit()
            else:
                conn.execute(
                    "UPDATE pump_grads SET candles_fetched=2, source='dead' WHERE mint=?",
                    (mint,),
                )
                conn.commit()
                done += 1

        await asyncio.gather(*(work(m, ts) for m, ts in tokens), return_exceptions=True)
        logger.info("GT lane finished: {} resolved (real+dead)", done)
        return done

    async def lane_gmgn(self, tokens: list[tuple[str, int]], conn) -> int:
        done = 0
        for mint, created_ts in reversed(tokens):
            if self._cached(mint):
                continue
            try:
                candles = await self.gmgn.klines(
                    mint, resolution="1m",
                    from_ts=max(created_ts - 1200, created_ts),
                    to_ts=created_ts + 6 * 3600,
                )
            except Exception as e:
                logger.debug("gmgn lane fail {}: {}", mint[:8], e)
                continue
            if candles and self._save(mint, candles, "gmgn", conn) is not None:
                done += 1
                conn.commit()
                logger.info("gmgn lane: {} done ({} total)", mint[:8], done)
        logger.info("GMGN lane finished: {} new", done)
        return done

    async def lane_helius(self, tokens: list[tuple[str, int]], conn, max_tokens: int = 500) -> int:
        from hunt.backtest.replay_prices import pool_price_series

        recheck = {
            m for (m,) in conn.execute(
                "SELECT mint FROM pump_grads WHERE source='dead_recheck' AND candles_fetched=0"
            ).fetchall()
        }
        done = 0
        slice_ = [t for t in reversed(tokens) if t[0] in recheck][:max_tokens] or \
                 list(reversed(tokens))[:max_tokens]
        skips = {"no_pool": 0, "no_candles": 0}
        client = httpx.AsyncClient(timeout=12)
        try:
            for mint, created_ts in slice_:
                if self._cached(mint):
                    continue
                try:
                    candles = await pool_price_series(mint, created_ts, client)
                except Exception as e:
                    logger.debug("helius lane fail {}: {}", mint[:8], e)
                    continue
                if candles:
                    if self._save(mint, candles, "onchain", conn) is not None:
                        done += 1
                        conn.commit()
                        logger.info("helius lane: {} done ({} total)", mint[:8], done)
                else:
                    skips["no_pool" if done >= 0 else "x"] += 0
                    skips["no_candles"] += 1
            logger.info("helius sweep stats: done={} skips={}", done, skips)
        finally:
            await client.aclose()
        return done

    async def verify_recent_deads(self, conn) -> int:
        import httpx as _hx

        rows = [dict(r) for r in conn.execute(
            "SELECT mint FROM pump_grads WHERE source='dead' AND candles_fetched=2"
        ).fetchall()]
        if not rows:
            return 0
        requeued = 0
        async with _hx.AsyncClient(timeout=10) as c:
            for r in rows:
                await self.gt_bucket.acquire()
                try:
                    resp = await c.get(
                        f"https://api.dexscreener.com/token-pairs/v1/solana/{r['mint']}"
                    )
                    pairs = resp.json() if resp.status_code == 200 else []
                    liq = max(
                        ((p.get("liquidity") or {}).get("usd") or 0) for p in pairs
                    ) if pairs else 0
                    if liq > 1000:
                        conn.execute(
                            "UPDATE pump_grads SET candles_fetched=0, source='dead_recheck' "
                            "WHERE mint=?",
                            (r["mint"],),
                        )
                        requeued += 1
                    else:
                        conn.execute(
                            "UPDATE pump_grads SET source='dead_verified' WHERE mint=?",
                            (r["mint"],),
                        )
                except Exception:
                    pass
                await asyncio.sleep(0.4)
            conn.commit()
        logger.info("verify-deads: {} checked | {} requeued | {} confirmed", len(rows), requeued,
                    len(rows) - requeued)
        return requeued

    async def run(self) -> None:
        round_no = 0
        while True:
            round_no += 1
            conn = _conn()
            rows = conn.execute(
                "SELECT mint, created_ts FROM pump_grads WHERE candles_fetched=0 ORDER BY created_ts DESC"
            ).fetchall()
            tokens = [(m, ts) for m, ts in rows]
            if not tokens:
                reconcile()
                logger.info("COLLECTOR COMPLETE: nothing left to fetch")
                conn.close()
                return
            recheck_n = conn.execute(
                "SELECT COUNT(*) FROM pump_grads WHERE source='dead_recheck' AND candles_fetched=0"
            ).fetchone()[0]
            mid = max(1, len(tokens) // 2)
            gt_slice = [t for t in tokens if t[0] not in set(
                m for (m,) in conn.execute(
                    "SELECT mint FROM pump_grads WHERE source='dead_recheck' AND candles_fetched=0"
                ).fetchall()
            )][:mid]
            tail_slice = tokens
            logger.info(
                "round {}: {} pending (recheck={}) → GT:{} HELius:{} GMGN:sweep",
                round_no, len(tokens), recheck_n, len(gt_slice), len(tail_slice),
            )
            t0 = time.time()
            await asyncio.gather(
                self.lane_gt(gt_slice, conn),
                self.lane_gmgn(tokens, conn),
                self.lane_helius(tail_slice, conn),
            )
            await self.verify_recent_deads(conn)
            reconcile()
            remaining = conn.execute(
                "SELECT COUNT(*) FROM pump_grads WHERE candles_fetched=0"
            ).fetchone()[0]
            conn.close()
            logger.info(
                "round {} done in {:.0f}s | remaining={} | by-src={}",
                round_no, time.time() - t0, remaining,
                dict(conn.execute(
                    "SELECT COALESCE(source,'?'), COUNT(*) FROM pump_grads GROUP BY source"
                ).fetchall()) if False else "see-db",
            )
            if remaining == 0:
                logger.info("COLLECTOR COMPLETE")
                return
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(KlineCollector().run())
