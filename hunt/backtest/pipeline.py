from __future__ import annotations

import asyncio
import time

from loguru import logger

from hunt.backtest.engine import DeepBacktester
from hunt.config import get_settings
from hunt.db.database import Database
from hunt.graph.builder import GraphBuilder
from hunt.notify.base import Notifier
from hunt.select.selector_v2 import SelectorV2
from hunt.universe.extractor import HybridExtractor, run_extraction_once


class Pipeline:
    def __init__(self, db: Database, notifier: Notifier) -> None:
        self.s = get_settings()
        self.db = db
        self.notifier = notifier
        self.graph = GraphBuilder(db)
        self.selector = SelectorV2(db, notifier)
        from hunt.gmgn.client import GmgnClient

        self.gmgn = GmgnClient(self.s.gmgn_api_key)

    async def _ingest_smartmoney(self) -> int:
        trades = await self.gmgn.smart_money_trades(limit=self.s.gmgn_smartmoney_limit)
        added = set()
        for t in trades:
            if not t.ts or t.side != "buy":
                continue
            day = time.strftime("%Y-%m-%d", time.gmtime(t.ts))
            await self.db.save_edge(t.wallet, t.mint, day, "gmgn_smartmoney", None)
            if t.wallet not in added:
                row = await self.db.get_wallet(t.wallet)
                if row is None:
                    await self.db.upsert_wallet(
                        t.wallet, "gmgn_smartmoney", first_token=t.mint
                    )
                added.add(t.wallet)
        await self.db.commit_edges()
        return len(added)

    async def _discover_top_traders(self) -> tuple[int, list[tuple[str, int]]]:
        from hunt.gmgn.discovery import GmgnDiscovery

        if not hasattr(self, "_discovery"):
            self._discovery = GmgnDiscovery(self.db, self.gmgn)
        return await self._discovery.discover_once()

    async def _prescreen_with_gmgn(self, wallets: list[str]) -> list[str]:
        try:
            pnl_map = await self.gmgn.batch_pnl(wallets[:100], self.s.gmgn_prescreen_period)
        except Exception as e:
            logger.warning("gmgn prescreen failed: {}", e)
            return wallets
        kept: list[str] = []
        dropped = 0
        for w in wallets:
            p = pnl_map.get(w)
            if p is None:
                kept.append(w)
                continue
            if p.realized_profit < self.s.gmgn_min_recent_pnl_sol:
                dropped += 1
                logger.info(
                    "prescreen drop {} ({}d pnl {:+.1f} SOL)",
                    w[:8], p.period, p.realized_profit,
                )
            else:
                kept.append(w)
        logger.info("gmgn prescreen: {} kept, {} dropped (negative recent pnl)", len(kept), dropped)
        return kept

    async def run_cycle(self) -> None:
        logger.info("pipeline cycle starting")
        sm_added = await self._ingest_smartmoney()
        if sm_added:
            logger.info("gmgn smart-money: {} new wallet(s)", sm_added)
        disc_added, multi = await self._discover_top_traders()
        if multi:
            logger.info(
                "gmgn discovery: {} new | multi-coin repeaters: {}",
                disc_added, ", ".join(f"{w[:8]}…×{n}" for w, n in multi[:5]),
            )
        await run_extraction_once(self.db)

        consistent = await self.graph.consistent_wallets()
        logger.info("graph: {} wallets with ≥{} distinct win-days",
                    len(consistent), self.s.graph_min_distinct_days)
        if not consistent:
            await self.notifier.send("📊 pipeline: no consistent wallets yet")
            return

        tracked = set(await self.db.tracked_wallet_addresses())
        targets = [g.wallet for g in consistent if g.wallet not in tracked]
        targets = await self._prescreen_with_gmgn(targets)
        bt = DeepBacktester(self.db)
        try:
            report = await bt.run_stage(targets[:20], self.s.backtest_stage_days)
        finally:
            await bt.close()

        ok = DeepBacktester(self.db).gate_ok(report)
        await self.selector.demote_failed_tracked()
        promoted = await self.selector.promote_composite()

        stage = self.s.backtest_stage_days
        next_stage = {2: 15, 15: 30}.get(stage)
        if ok and next_stage and promoted is not None:
            logger.info(
                "gate passed at {}d — bump BACKTEST_STAGE_DAYS to {} when ready",
                stage, next_stage,
            )
            await self.notifier.send(
                f"📈 stage {stage}d done: parse {report['parse_rate']:.0%}, "
                f"{len(report['qualified'])} qualified, {len(promoted)} newly tracked.\n"
                f"Ready to widen window → set HUNT_BACKTEST_STAGE_DAYS={next_stage}"
            )
        elif not ok:
            await self.notifier.send(
                f"⚠️ stage {stage}d gate failed (parse {report['parse_rate']:.0%}, "
                f"bots {report['bot_ratio']:.0%}) — staying at this depth"
            )

    async def run(self) -> None:
        while True:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.exception("pipeline loop error: {}", e)
            await asyncio.sleep(self.s.backtest_interval_h * 3600)


async def run_pipeline(db: Database, notifier: Notifier) -> None:
    p = Pipeline(db, notifier)
    await p.run()
