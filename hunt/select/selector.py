from __future__ import annotations

import asyncio
import json
import time

from loguru import logger

from hunt.config import get_settings
from hunt.db.database import Database
from hunt.notify.base import Notifier


class Selector:
    def __init__(self, db: Database, notifier: Notifier) -> None:
        self.s = get_settings()
        self.db = db
        self.notifier = notifier

    async def select_once(self) -> tuple[list[str], list[str]]:
        promoted: list[str] = []
        demoted: list[str] = []

        slots = self.s.max_tracked_wallets - len(await self.db.tracked_wallet_addresses())
        if slots > 0:
            candidates = await self.db.top_candidates(slots * 3)
            for row in candidates:
                if slots <= 0:
                    break
                metrics = json.loads(row["metrics_json"] or "{}")
                if not metrics.get("qualified"):
                    continue
                bt = await self.db.latest_backtest(row["address"])
                if bt:
                    try:
                        bt_metrics = json.loads(bt["metrics_json"] or "{}")
                    except Exception:
                        bt_metrics = {}
                    if not bt_metrics.get("qualified"):
                        continue
                await self.db.set_wallet_status(row["address"], "tracked")
                promoted.append(row["address"])
                slots -= 1
                logger.info("promoted {} score={:.2f}", row["address"][:8], row["score"] or 0)

        return promoted, demoted

    async def run(self) -> None:
        while True:
            try:
                await self.select_once()
            except Exception as e:
                logger.exception("selector loop error: {}", e)
            await asyncio.sleep(self.s.selector_interval_s)


async def run_selector(db: Database, notifier: Notifier) -> None:
    sel = Selector(db, notifier)
    await sel.run()
