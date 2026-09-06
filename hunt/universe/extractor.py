from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx
from loguru import logger

from hunt.config import get_settings
from hunt.db.database import Database
from hunt.scout.birdeye import Birdeye
from hunt.scout.dexscreener import DexScreener
from hunt.utils.concurrency import GlobalRateBucket, worker_pool_sem
from hunt.utils.http import post_json_rpc
from hunt.utils.swaps import (
    WalletTradeLike,
    deltas_from_ws_notification,
    extract_swap_for_owner,
)


@dataclass
class WinDayTrader:
    wallet: str
    mint: str
    day: str
    pnl_sol: float
    volume_sol: float


class HybridExtractor:
    def __init__(self, db: Database) -> None:
        s = get_settings()
        self.s = s
        self.db = db
        self.http = httpx.AsyncClient()
        self.bucket = GlobalRateBucket(s.rpc_global_rps, s.rpc_global_burst)
        self.sem = worker_pool_sem(4)
        self.birdeye = Birdeye(self.http, s.birdeye_api_key, db)
        self.ds = DexScreener(self.http)

    async def close(self) -> None:
        await self.http.aclose()

    async def extract_cheap(self, mint: str, day: str, symbol: str | None) -> list[tuple[str, int]]:
        wallets: list[str] = []
        seen: set[str] = set()
        for tf in ("7d", "30d"):
            for offset in (0, 10):
                traders = await self.birdeye.top_traders(mint, time_frame=tf, offset=offset)
                for t in traders:
                    o = t["owner"]
                    if o not in seen:
                        seen.add(o)
                        wallets.append(o)
        ranked = [(w, i + 1) for i, w in enumerate(wallets)]
        for w, rank in ranked:
            await self.db.save_edge(w, mint, day, "birdeye", rank)
        await self.db.commit_edges()
        _ = symbol
        return ranked

    async def pool_address(self, mint: str) -> str | None:
        pairs = await self.ds.pairs_for_mints([mint])
        ht = pairs.get(mint)
        return ht.pair_address if ht else None

    async def _page_signatures_until(
        self, address: str, stop_before_ts: int, max_pages: int
    ) -> list[dict]:
        collected: list[dict] = []
        before: str | None = None
        for _ in range(max_pages):
            await self.bucket.acquire()
            params: dict = {"limit": 1000}
            if before:
                params["before"] = before
            resp = await post_json_rpc(
                self.http, get_settings().rpc_http,
                "getSignaturesForAddress", [address, params],
            )
            if not resp or "result" not in resp:
                break
            entries = resp["result"] or []
            if not entries:
                break
            reached_stop = False
            for e in entries:
                bt = e.get("blockTime") or 0
                if bt and bt < stop_before_ts:
                    reached_stop = True
                    continue
                if not e.get("err"):
                    collected.append(e)
            before = entries[-1].get("signature")
            if reached_stop or len(entries) < 1000:
                break
        return collected

    async def win_day_pnl_leaderboard(
        self, mint: str, day_start_ts: int, day_end_ts: int
    ) -> list[WinDayTrader]:
        pool = await self.pool_address(mint)
        if not pool:
            return []
        sigs = await self._page_signatures_until(
            pool, day_start_ts, self.s.precise_max_sigs_pages
        )
        in_window = [
            e for e in sigs
            if day_start_ts <= (e.get("blockTime") or 0) <= day_end_ts
        ]
        logger.debug(
            "win-day {}: {} sigs paged, {} in window", day_start_ts, len(sigs), len(in_window)
        )

        per_wallet: dict[str, dict[str, float]] = {}
        fetched = 0
        for chunk_start in range(0, len(in_window), 4):
            if fetched >= self.s.precise_max_txs:
                break
            chunk = in_window[chunk_start : chunk_start + 4]

            async def fetch_one(e: dict):
                nonlocal fetched
                async with self.sem:
                    await self.bucket.acquire()
                    resp = await post_json_rpc(
                        self.http, get_settings().rpc_http, "getTransaction",
                        [e["signature"], {
                            "encoding": "jsonParsed",
                            "commitment": "confirmed",
                            "maxSupportedTransactionVersion": 0,
                        }],
                    )
                fetched += 1
                return resp

            results = await asyncio.gather(*(fetch_one(e) for e in chunk))
            for resp in results:
                if not resp or not resp.get("result"):
                    continue
                r = resp["result"]
                meta = r.get("meta") or {}
                if meta.get("err"):
                    continue
                deltas = deltas_from_ws_notification({
                    "transaction": {"transaction": r.get("transaction"), "meta": meta}
                })
                owners = {o for (o, _m) in deltas.token_deltas}
                for owner in owners:
                    swaps = extract_swap_for_owner(deltas, owner)
                    for sw in swaps:
                        agg = per_wallet.setdefault(owner, {"pnl": 0.0, "vol": 0.0})
                        if sw.side == "BUY":
                            agg["pnl"] -= sw.sol_amount
                        else:
                            agg["pnl"] += sw.sol_amount
                        agg["vol"] += sw.sol_amount

        day_str = time.strftime("%Y-%m-%d", time.gmtime(day_start_ts))
        traders = [
            WinDayTrader(w, mint, day_str, v["pnl"], v["vol"])
            for w, v in per_wallet.items()
            if v["vol"] > 0.5
        ]
        traders.sort(key=lambda t: t.pnl_sol, reverse=True)
        top = traders[: self.s.precise_top_wallets]
        for i, t in enumerate(top):
            if t.pnl_sol > 0:
                await self.db.save_edge(t.wallet, mint, day_str, "onchain", i + 1)
        await self.db.commit_edges()
        return top

    async def run_precise_for_universe(self, limit_tokens: int | None = None) -> int:
        tokens = await self.db.universe_tokens()
        strongest = tokens[: limit_tokens or self.s.extraction_precise_top_tokens]
        added_edges = 0
        for row in strongest:
            mint = row["mint"]
            days = await self.db.universe_token_days(mint)
            best_day = days[0] if days else None
            if not best_day:
                continue
            day_ts = int(time.mktime(time.strptime(best_day["day"], "%Y-%m-%d"))) - _tz_offset()
            leaders = await self.win_day_pnl_leaderboard(
                mint, day_ts, day_ts + 86400
            )
            added_edges += sum(1 for t in leaders if t.pnl_sol > 0)
            logger.info(
                "precise extraction {}: day-{} → {} profitable leaders",
                (row["symbol"] or mint[:8]), best_day["day"],
                sum(1 for t in leaders if t.pnl_sol > 0),
            )
        return added_edges


def _tz_offset() -> int:
    return 0


async def run_extraction_once(db: Database) -> None:
    ex = HybridExtractor(db)
    try:
        tokens = await db.universe_tokens()
        cheap_edges = 0
        for row in tokens:
            days = await db.universe_token_days(row["mint"])
            for d in days[:2]:
                ranked = await ex.extract_cheap(row["mint"], d["day"], row["symbol"])
                cheap_edges += len(ranked)
        logger.info("cheap layer done: {} edges from {} tokens", cheap_edges, len(tokens))
        precise = await ex.run_precise_for_universe()
        logger.info("precise layer done: {} on-chain leader edges", precise)
    finally:
        await ex.close()
