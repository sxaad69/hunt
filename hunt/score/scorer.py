from __future__ import annotations

import asyncio
import json
import time

import httpx
from loguru import logger

from hunt.config import get_settings
from hunt.db.database import Database
from hunt.notify.base import Notifier
from hunt.score.metrics import compute_metrics
from hunt.score.replay import ReplayEngine


class Scorer:
    def __init__(self, db: Database, notifier: Notifier) -> None:
        self.s = get_settings()
        self.db = db
        self.notifier = notifier
        self.client = httpx.AsyncClient()
        self.replay = ReplayEngine(self.client, db)

    async def close(self) -> None:
        await self.client.aclose()

    async def score_wallet(self, address: str) -> dict | None:
        trades = await self.replay.fetch_history(address)
        if not trades:
            metrics = {"reasons": ["mev_or_inactive"], "qualified": False}
            await self.db.update_wallet_score(address, -999.0, metrics)
            await self.db.set_wallet_status(address, "rejected")
            logger.info("rejected {} (no copyable swaps — bot/inactive)", address[:8])
            return None
        from hunt.db.database import WalletTrade

        inserted = await self.db.save_wallet_trades(
            address,
            [
                WalletTrade(
                    signature=t.signature,
                    ts=t.ts,
                    mint=t.mint,
                    symbol=None,
                    side=t.side,
                    sol_amount=t.sol_amount,
                    token_amount=t.token_amount,
                )
                for t in trades
            ],
        )
        stored = await self.db.get_wallet_trades(address)
        m = compute_metrics(stored)
        await self.db.update_wallet_score(address, m.score, m.summary())
        last_active = max((t.ts for t in trades), default=None)
        if last_active:
            await self.db.db.execute(
                "UPDATE wallets SET last_active_ts=? WHERE address=?", (last_active, address)
            )
            await self.db.db.commit()
        logger.info(
            "scored {} trades={} win={:.0%} pnl={:.2f} score={:.2f} qualified={} new_fills={}",
            address[:8], m.trades, m.win_rate, m.realized_pnl_sol, m.score,
            m.qualified, inserted,
        )
        return m.summary()

    async def score_batch(self) -> int:
        candidates = await self.db.get_wallets_by_status("candidate")
        unscored = [w for w in candidates if w["scored_at"] is None]
        stale = sorted(
            [w for w in candidates if w["scored_at"] is not None],
            key=lambda w: w["scored_at"] or 0,
        )
        queue = unscored + stale
        budget = self.s.daily_wallet_score_budget
        rescore_horizon = time.time() - self.s.rescore_after_h * 3600
        scored = 0
        for w in queue:
            if scored >= budget:
                break
            used_today = await self.db.kv_get_int(f"wallets_scored:{time.strftime('%Y-%m-%d')}")
            if used_today >= budget:
                break
            if w["scored_at"] is not None and w["scored_at"] > rescore_horizon:
                continue
            try:
                summary = await self.score_wallet(w["address"])
                scored += 1
                await self.db.kv_bump_daily("wallets_scored")
                if summary and summary.get("qualified"):
                    await self.notifier.send(
                        f"✅ qualified wallet {w['address'][:8]}… "
                        f"win {summary['win_rate']:.0%} | pnl {summary['realized_pnl_sol']} SOL "
                        f"| trades {summary['trades']} | score {summary['score']}"
                    )
            except Exception as e:
                logger.warning("scoring failed {}: {}", w["address"][:8], e)
            await asyncio.sleep(1)
        return scored

    async def run(self) -> None:
        while True:
            try:
                n = await self.score_batch()
                if n:
                    logger.info("scorer batch done: {} wallets", n)
            except Exception as e:
                logger.exception("scorer loop error: {}", e)
            await asyncio.sleep(self.s.scorer_interval_s)


async def run_scorer(db: Database, notifier: Notifier) -> None:
    scorer = Scorer(db, notifier)
    try:
        await scorer.run()
    finally:
        await scorer.close()
