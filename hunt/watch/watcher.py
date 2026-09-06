from __future__ import annotations

import asyncio

from loguru import logger

from hunt.config import get_settings
from hunt.db.database import Database
from hunt.exec.executor import TradeExecutor
from hunt.notify.base import AppState, Notifier
from hunt.risk.engine import RiskEngine
from hunt.risk.stops import StopsMonitor
from hunt.watch.helius_ws import HeliusTxStream
from hunt.watch.parser import dedupe, parse_notification


class Watcher:
    def __init__(
        self,
        db: Database,
        notifier: Notifier,
        state: AppState,
        executor: TradeExecutor,
    ) -> None:
        self.s = get_settings()
        self.db = db
        self.notifier = notifier
        self.state = state
        self.executor = executor
        self.risk = RiskEngine(db, notifier, state)
        self.stops = StopsMonitor(db, notifier, executor)
        self.stream: HeliusTxStream | None = None
        self._current_wallets: set[str] = set()

    async def _sync_subscriptions(self) -> None:
        wallets = set(await self.db.tracked_wallet_addresses())
        if wallets == self._current_wallets:
            return
        self._current_wallets = wallets
        if self.stream is None:
            if not wallets:
                return
            self.stream = HeliusTxStream(self.s.rpc_ws, self.s.rpc_http, sorted(wallets))
            asyncio.get_running_loop().create_task(self._consume())
            asyncio.get_running_loop().create_task(self.stream.run())
            logger.info("watcher started with {} wallets", len(wallets))
        else:
            self.stream.wallets = sorted(wallets)
            self.stream.request_reload()
            logger.info("watcher subscriptions updated: {} wallets", len(wallets))

    async def _consume(self) -> None:
        assert self.stream is not None
        seen: set[str] = set()
        while True:
            result = await self.stream.queue.get()
            try:
                signals = dedupe(parse_notification(result, self._current_wallets), seen)
                for sig in signals:
                    await self._handle_signal(sig)
            except Exception as e:
                logger.exception("signal handling error: {}", e)

    async def _handle_signal(self, sig) -> None:
        logger.info(
            "SIGNAL {} {} amt={:.2f} tokens sol={:.3f} by {}",
            sig.side, sig.mint[:8], sig.token_amount, sig.sol_amount, sig.wallet[:8],
        )
        action, reason, meta = await self.risk.evaluate(sig)
        await self.db.insert_signal(
            sig.wallet, sig.mint, None, sig.side, sig.sol_amount, action, reason
        )
        if action == "copy_buy":
            await self.executor.copy_buy(sig)
        elif action == "copy_sell":
            await self.executor.copy_sell(sig, meta.get("fraction", 1.0))
        elif action == "skip" and reason:
            logger.debug("skipped {}: {}", sig.side, reason)

    async def run(self) -> None:
        stops_task = asyncio.get_running_loop().create_task(self.stops.run())
        while True:
            try:
                await self._sync_subscriptions()
            except Exception as e:
                logger.exception("subscription sync error: {}", e)
            await asyncio.sleep(30)


async def run_watcher(db: Database, notifier: Notifier, state: AppState, executor: TradeExecutor) -> Watcher:
    w = Watcher(db, notifier, state, executor)
    await w.run()
