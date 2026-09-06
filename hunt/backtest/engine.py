from __future__ import annotations

import asyncio
import json
import time

import httpx
from loguru import logger

from hunt.config import get_settings
from hunt.db.database import Database
from hunt.score.metrics import build_closed_trades, compute_metrics
from hunt.utils.concurrency import GlobalRateBucket
from hunt.utils.http import post_json_rpc
from hunt.utils.swaps import WalletTradeLike, deltas_from_ws_notification, extract_swap_for_owner


class DeepBacktester:
    def __init__(self, db: Database) -> None:
        s = get_settings()
        self.s = s
        self.db = db
        self.http = httpx.AsyncClient()
        self.bucket = GlobalRateBucket(s.rpc_global_rps, s.rpc_global_burst)

    async def close(self) -> None:
        await self.http.aclose()

    async def replay_window(
        self, owner: str, window_days: int, max_txs: int | None = None
    ) -> tuple[list[WalletTradeLike], int]:
        max_txs = max_txs or self.s.backtest_window_cap_txs
        cutoff_ts = int(time.time()) - window_days * 86400

        collected_sigs: list[tuple[str, int]] = []
        before: str | None = None
        reached_cutoff = False
        while len(collected_sigs) < max_txs * 2:
            await self.bucket.acquire()
            params: dict = {"limit": 1000}
            if before:
                params["before"] = before
            resp = await post_json_rpc(
                self.http, self.s.rpc_http,
                "getSignaturesForAddress", [owner, params],
            )
            if not resp or "result" not in resp:
                break
            entries = resp["result"] or []
            if not entries:
                break
            for e in entries:
                bt = e.get("blockTime") or 0
                if e.get("err"):
                    continue
                if bt and bt < cutoff_ts:
                    reached_cutoff = True
                    continue
                collected_sigs.append((e["signature"], bt))
            before = entries[-1].get("signature")
            if reached_cutoff or len(entries) < 1000:
                break

        trades: list[WalletTradeLike] = []
        seen: set[str] = set()
        for sig, bt in collected_sigs[:max_txs]:
            if sig in seen:
                continue
            seen.add(sig)
            await self.bucket.acquire()
            resp = await post_json_rpc(
                self.http, self.s.rpc_http, "getTransaction",
                [sig, {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0,
                }],
            )
            if not resp or not resp.get("result"):
                continue
            r = resp["result"]
            meta = r.get("meta") or {}
            if meta.get("err"):
                continue
            deltas = deltas_from_ws_notification({
                "transaction": {"transaction": r.get("transaction"), "meta": meta}
            })
            for sw in extract_swap_for_owner(deltas, owner):
                trades.append(WalletTradeLike(sig, bt, sw.mint, sw.side, sw.sol_amount, sw.token_amount))

        return trades, len(collected_sigs)

    async def backtest_wallet(self, wallet: str, stage_days: int) -> dict | None:
        trades, sig_count = await self.replay_window(wallet, stage_days)
        closed, open_bags = build_closed_trades(trades)
        m = compute_metrics(trades)
        summary = m.summary()
        summary["window_days"] = stage_days
        summary["sig_scanned"] = sig_count
        summary["open_bags"] = len(open_bags)

        await self.db.save_backtest_run(
            stage_days, wallet, m.trades, m.win_rate, m.realized_pnl_sol,
            json.dumps(summary),
        )
        logger.info(
            "backtest[{}d] {} trades={} win={:.0%} pnl={:.2f} qualified={}",
            stage_days, wallet[:8], m.trades, m.win_rate, m.realized_pnl_sol, m.qualified,
        )
        return summary

    async def run_stage(self, wallets: list[str], stage_days: int) -> dict:
        results: dict[str, dict] = {}
        parsed = 0
        for w in wallets:
            try:
                summary = await self.backtest_wallet(w, stage_days)
                if summary:
                    results[w] = summary
                    if summary["trades"] > 0:
                        parsed += 1
            except Exception as e:
                logger.warning("backtest failed {}: {}", w[:8], e)
            await asyncio.sleep(0.5)

        parse_rate = parsed / len(wallets) if wallets else 0.0
        bot_ratio = (
            sum(1 for r in results.values() if r["trades"] == 0) / len(results)
            if results else 0.0
        )
        report = {
            "stage_days": stage_days,
            "wallets": len(wallets),
            "parsed": parsed,
            "parse_rate": round(parse_rate, 2),
            "bot_ratio": round(bot_ratio, 2),
            "qualified": [
                {"wallet": w, **{k: r[k] for k in ("trades", "win_rate", "realized_pnl_sol", "score")}}
                for w, r in results.items() if r.get("qualified")
            ],
        }
        logger.info("STAGE {}d complete: {}", stage_days, json.dumps(report)[:400])
        return report

    def gate_ok(self, report: dict) -> bool:
        return (
            report["parse_rate"] >= self.s.backtest_parse_rate_gate
            and report["bot_ratio"] <= 0.7
        )


async def run_backtest_stage(db: Database, wallets: list[str], stage_days: int) -> dict:
    bt = DeepBacktester(db)
    try:
        return await bt.run_stage(wallets, stage_days)
    finally:
        await bt.close()
