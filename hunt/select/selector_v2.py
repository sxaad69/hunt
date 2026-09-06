from __future__ import annotations

import json

from loguru import logger

from hunt.config import get_settings
from hunt.db.database import Database
from hunt.graph.builder import GraphBuilder
from hunt.notify.base import Notifier


class SelectorV2:
    def __init__(self, db: Database, notifier: Notifier) -> None:
        self.s = get_settings()
        self.db = db
        self.notifier = notifier
        self.graph = GraphBuilder(db)

    async def demote_failed_tracked(self) -> list[str]:
        demoted: list[str] = []
        for row in await self.db.get_wallets_by_status("tracked"):
            bt = await self.db.latest_backtest(row["address"])
            if not bt:
                continue
            try:
                metrics = json.loads(bt["metrics_json"] or "{}")
            except Exception:
                continue
            if metrics.get("qualified"):
                continue
            if metrics.get("trades", 0) == 0 and "mev_or_inactive" in (
                json.loads(row["metrics_json"] or "{}").get("reasons") or []
            ):
                continue
            await self.db.set_wallet_status(row["address"], "candidate")
            demoted.append(row["address"])
            logger.warning(
                "demoted {} (60d verdict failed: {})",
                row["address"][:8], (metrics.get("reasons") or [])[:2],
            )
        return demoted

    async def promote_composite(self) -> list[str]:
        promoted: list[str] = []
        slots = self.s.max_tracked_wallets - len(await self.db.tracked_wallet_addresses())
        if slots <= 0:
            return promoted

        consistent = await self.graph.consistent_wallets()
        ranked: list[tuple[float, str, dict]] = []
        for g in consistent:
            if g.wallet in await self.db.tracked_wallet_addresses():
                continue
            row = await self.db.get_wallet(g.wallet)
            if row and row["status"] == "rejected":
                continue
            bt = await self.db.latest_backtest(g.wallet)
            if not bt:
                continue
            try:
                metrics = json.loads(bt["metrics_json"] or "{}")
            except Exception:
                continue
            if not metrics.get("qualified"):
                continue
            bt_score = max(0.0, min(1.0, float(metrics.get("score") or 0)))
            composite = 0.5 * g.consistency_score + 0.5 * bt_score
            ranked.append((composite, g.wallet, metrics))

        ranked.sort(key=lambda x: x[0], reverse=True)
        for composite, wallet, metrics in ranked[:slots]:
            import time as _t

            await self.db.db.execute(
                """INSERT INTO wallets(address, source, status, added_at)
                   VALUES(?,?, 'tracked', ?)
                   ON CONFLICT(address) DO UPDATE SET status='tracked'""",
                (wallet, "selector_v2", int(_t.time())),
            )
            await self.db.db.commit()
            promoted.append(wallet)
            logger.info("promoted[{}] {} composite={:.2f}", self.s.backtest_stage_days,
                        wallet[:8], composite)

        if promoted:
            lines = [f"➕ tracking: {w[:8]}… (composite)" for w in promoted]
            await self.notifier.send("\n".join(lines))
        return promoted
