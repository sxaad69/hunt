from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger

DEMO_KEY = "gmgn_solbscbaseethmonadtron"


@dataclass
class SmartTrade:
    wallet: str
    mint: str
    symbol: str | None
    side: str
    amount_usd: float
    ts: int


@dataclass
class WalletPnl:
    wallet: str
    realized_profit: float
    buy_count: int
    sell_count: int
    period: str


class GmgnClient:
    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key or DEMO_KEY
        self._cli: list[str] | None = None
        from hunt.utils.concurrency import GlobalRateBucket

        self.bucket = GlobalRateBucket(rate_per_sec=1.0, burst=2)

    def _resolve_cli(self) -> list[str]:
        if self._cli is None:
            direct = shutil.which("gmgn-cli")
            if direct:
                self._cli = [direct]
            else:
                npx = shutil.which("npx")
                if not npx:
                    raise RuntimeError("gmgn-cli not found and npx unavailable")
                self._cli = [npx, "-y", "gmgn-cli"]
        return self._cli

    async def _run(self, args: list[str], timeout_s: float = 45) -> Optional[dict]:
        cmd = self._resolve_cli() + args + ["--raw"]
        env = {**os.environ, "GMGN_API_KEY": self.api_key}
        for attempt in range(3):
            await self.bucket.acquire()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            except asyncio.TimeoutError:
                logger.warning("gmgn-cli timeout: {}", " ".join(args[:3]))
                return None
            except Exception as e:
                logger.warning("gmgn-cli spawn failed: {}", e)
                return None
            if proc.returncode != 0:
                err_text = err.decode()[:200]
                if "429" in err_text or "RATE_LIMIT" in err_text or "banned" in err_text.lower():
                    wait = 90 * (attempt + 1)
                    logger.warning("gmgn rate-limited — cooling down {}s", wait)
                    await asyncio.sleep(wait)
                    continue
                logger.debug("gmgn-cli rc={} err={}", proc.returncode, err_text)
                return None
            try:
                return json.loads(out.decode().strip().splitlines()[-1])
            except Exception:
                logger.debug("gmgn-cli unparseable output: {}", out.decode()[:150])
                return None
        return None

    async def smart_money_trades(self, chain: str = "sol", limit: int = 100) -> list[SmartTrade]:
        data = await self._run(["track", "smartmoney", "--chain", chain, "--limit", str(limit)])
        trades: list[SmartTrade] = []
        for t in (data or {}).get("list") or []:
            maker = t.get("maker")
            base = t.get("base_address")
            if not maker or not base:
                continue
            sym = ((t.get("base_token") or {}).get("symbol")) or None
            trades.append(
                SmartTrade(
                    wallet=maker,
                    mint=base,
                    symbol=sym,
                    side=t.get("side") or "",
                    amount_usd=float(t.get("amount_usd") or 0),
                    ts=int(t.get("timestamp") or 0),
                )
            )
        return trades

    async def batch_pnl(self, wallets: list[str], period: str = "30d") -> dict[str, WalletPnl]:
        out: dict[str, WalletPnl] = {}
        for i in range(0, len(wallets), 100):
            chunk = wallets[i : i + 100]
            args = ["portfolio", "profits", "--chain", "sol", "--period", period]
            for w in chunk:
                args += ["--wallet", w]
            data = await self._run(args, timeout_s=60)
            for row in (data or {}).get("list") or []:
                addr = row.get("wallet_address")
                if not addr:
                    continue
                out[addr] = WalletPnl(
                    wallet=addr,
                    realized_profit=float(row.get("realized_profit") or 0),
                    buy_count=int(row.get("buy") or 0),
                    sell_count=int(row.get("sell") or 0),
                    period=period,
                )
        return out

    async def token_top_traders(self, mint: str, limit: int = 30) -> list[dict]:
        data = await self._run([
            "token", "traders", "--chain", "sol", "--address", mint,
            "--limit", str(limit), "--order-by", "profit",
        ])
        return (data or {}).get("list") or []

    async def klines(
        self, mint: str, resolution: str = "1m", from_ts: int = 0, to_ts: int = 0
    ) -> list[dict]:
        res_seconds = {"30s": 30, "1m": 60, "5m": 300, "15m": 900, "1h": 3600,
                       "4h": 14400, "1d": 86400}.get(resolution, 60)
        page_span = min(100 * res_seconds, 6 * 3600)
        out: dict[int, dict] = {}
        t = from_ts
        while t < to_ts:
            end = min(t + page_span, to_ts)
            data = await self._run([
                "market", "kline", "--chain", "sol", "--address", mint,
                "--resolution", resolution,
                "--from", str(t), "--to", str(end),
            ])
            items = []
            if isinstance(data, dict):
                inner = data.get("data") or {}
                if isinstance(inner, dict):
                    items = (inner.get("list") or inner.get("klines")
                             or inner.get("items")) or []
            for it in items:
                ts_ms = int(it.get("time") or 0)
                if ts_ms:
                    out[ts_ms // 1000] = {
                        "time": ts_ms // 1000,
                        "open": float(it.get("open") or 0),
                        "high": float(it.get("high") or 0),
                        "low": float(it.get("low") or 0),
                        "close": float(it.get("close") or 0),
                        "volume": float(it.get("volume") or 0),
                    }
            t = end + 1
            await asyncio.sleep(0.2)
        return [out[k] for k in sorted(out)]

    async def trending(self, chain: str = "sol", interval: str = "1h", limit: int = 50) -> list[dict]:
        data = await self._run([
            "market", "trending", "--chain", chain,
            "--interval", interval, "--limit", str(limit),
        ])
        return (data or {}).get("rank") or []

    async def wallet_activity(self, wallet: str) -> list[dict]:
        data = await self._run(["portfolio", "activity", "--chain", "sol", "--wallet", wallet])
        return (data or {}).get("list") or (data or {}).get("activities") or []


def day_of_ts(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))
