from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

from hunt.config import get_settings
from hunt.utils.concurrency import GlobalRateBucket, worker_pool_sem
from hunt.utils.http import fetch_json

BASE = "https://public-api.birdeye.so"


@dataclass
class DailyCandle:
    ts: int
    open: float
    close: float
    volume_usd: float


class OhlcvCache:
    def __init__(self, cache_dir: str | Path, ttl_h: float) -> None:
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ttl_s = ttl_h * 3600

    def _path(self, mint: str) -> Path:
        return self.dir / f"{mint}.json"

    def get(self, mint: str) -> Optional[list[dict]]:
        p = self._path(mint)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
            if time.time() - data.get("fetched_at", 0) > self.ttl_s:
                return None
            return data.get("candles")
        except Exception:
            return None

    def put(self, mint: str, candles: list[dict]) -> None:
        try:
            self._path(mint).write_text(
                json.dumps({"fetched_at": time.time(), "candles": candles})
            )
        except Exception as e:
            logger.debug("cache write failed {}: {}", mint[:8], e)


class OhlcvFetcher:
    def __init__(self, client: httpx.AsyncClient, db, bucket: GlobalRateBucket | None = None) -> None:
        s = get_settings()
        self.s = s
        self.client = client
        self.db = db
        self.bucket = bucket or GlobalRateBucket(s.rpc_global_rps * 0.5, 8)
        self.sem = worker_pool_sem(s.ohlcv_workers)
        self.cache = OhlcvCache(f"{s.data_dir}/cache/ohlcv", s.ohlcv_cache_ttl_h)

    def _headers(self) -> dict:
        return {"X-API-KEY": self.s.birdeye_api_key, "x-chain": "solana"}

    def parse_candles(self, items: list[dict]) -> list[DailyCandle]:
        out: list[DailyCandle] = []
        for it in items:
            try:
                o = float(it.get("o") or it.get("open") or 0)
                c = float(it.get("c") or it.get("close") or 0)
                v = float(it.get("v") or it.get("volume") or 0)
                ts = int(it.get("unixTime") or 0)
                if o > 0 and c > 0 and ts > 0:
                    out.append(DailyCandle(ts, o, c, v))
            except Exception:
                continue
        return out

    async def fetch_candles(self, mint: str, days: int) -> list[DailyCandle]:
        cached = self.cache.get(mint)
        if cached is not None:
            return self.parse_candles(cached)

        now = int(time.time())
        time_from = now - days * 86400
        raw = await self.fetch_raw(mint, from_ts=time_from, to_ts=now, interval="1D")
        if raw is None:
            return []
        await self.db.kv_bump_daily("birdeye_cu", 4)
        candles = self.parse_candles(raw)
        self.cache.put(mint, raw)
        return candles

    async def fetch_raw(
        self, mint: str, from_ts: int, to_ts: int, interval: str = "1D"
    ) -> Optional[list[dict]]:
        cached = self.cache.get(mint)
        if cached is not None and interval == "1D":
            return cached
        async with self.sem:
            await self.bucket.acquire()
            if not await self._cu_ok():
                return None
            data = await fetch_json(
                self.client, "GET", f"{BASE}/defi/ohlcv",
                params={"address": mint, "type": interval,
                        "time_from": str(from_ts), "time_to": str(to_ts)},
                headers=self._headers(),
            )
        if not data:
            return None
        await self.db.kv_bump_daily("birdeye_cu", 4)
        raw = ((data.get("data") or {}).get("items")) or []
        if interval == "1D":
            self.cache.put(mint, raw)
        else:
            p = self.cache.dir / f"{mint}_{interval}.json"
            try:
                p.write_text(json.dumps({"fetched_at": time.time(), "candles": raw}))
            except Exception:
                pass
        return raw

    async def _cu_ok(self) -> bool:
        used = await self.db.kv_get_int(f"birdeye_cu:{_today()}")
        return used + 4 <= self.s.birdeye_daily_cu_budget


def _today() -> str:
    return time.strftime("%Y-%m-%d")
