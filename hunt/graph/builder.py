from __future__ import annotations

from dataclasses import dataclass

from hunt.config import get_settings
from hunt.db.database import Database


@dataclass
class WalletGraphStats:
    wallet: str
    tokens: int
    days: int
    edges: int
    consistency_score: float


class GraphBuilder:
    def __init__(self, db: Database) -> None:
        self.s = get_settings()
        self.db = db

    async def compute(self) -> list[WalletGraphStats]:
        rows = await self.db.wallet_edge_stats()
        out: list[WalletGraphStats] = []
        for r in rows:
            days = r["days"]
            score = min(1.0, days / max(1, self.s.graph_min_distinct_days * 3)) * 0.6 + \
                min(1.0, r["tokens"] / 10.0) * 0.4
            out.append(WalletGraphStats(r["wallet"], r["tokens"], days, r["edges"], round(score, 3)))
        out.sort(key=lambda x: x.consistency_score, reverse=True)
        return out

    async def consistent_wallets(self, min_days: int | None = None) -> list[WalletGraphStats]:
        stats = await self.compute()
        threshold = min_days if min_days is not None else self.s.graph_min_distinct_days
        return [s for s in stats if s.days >= threshold]
