from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Optional

from hunt.config import get_settings
from hunt.utils.swaps import WalletTradeLike


@dataclass
class ClosedTrade:
    mint: str
    entry_ts: int
    exit_ts: int
    sol_in: float
    sol_out: float

    @property
    def pnl(self) -> float:
        return self.sol_out - self.sol_in

    @property
    def hold_s(self) -> int:
        return max(0, self.exit_ts - self.entry_ts)


@dataclass
class WalletMetrics:
    trades: int = 0
    wins: int = 0
    win_rate: float = 0.0
    realized_pnl_sol: float = 0.0
    avg_pnl_sol: float = 0.0
    median_hold_min: float = 0.0
    distinct_tokens: int = 0
    instant_sell_ratio: float = 0.0
    profitable_token_ratio: float = 0.0
    open_bags: int = 0
    score: float = -999.0
    qualified: bool = False
    reasons: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "trades": self.trades,
            "win_rate": round(self.win_rate, 3),
            "realized_pnl_sol": round(self.realized_pnl_sol, 3),
            "avg_pnl_sol": round(self.avg_pnl_sol, 4),
            "median_hold_min": round(self.median_hold_min, 1),
            "distinct_tokens": self.distinct_tokens,
            "instant_sell_ratio": round(self.instant_sell_ratio, 3),
            "profitable_token_ratio": round(self.profitable_token_ratio, 2),
            "open_bags": self.open_bags,
            "score": round(self.score, 3),
            "qualified": self.qualified,
            "reasons": self.reasons,
        }


def build_closed_trades(trades: list[WalletTradeLike]) -> tuple[list[ClosedTrade], dict[str, float]]:
    per_mint: dict[str, list[WalletTradeLike]] = {}
    for t in sorted(trades, key=lambda x: x.ts):
        per_mint.setdefault(t.mint, []).append(t)

    closed: list[ClosedTrade] = []
    open_bags: dict[str, float] = {}
    for mint, fills in per_mint.items():
        queue: list[tuple[float, int]] = []
        for f in fills:
            if f.side == "BUY":
                queue.append((f.sol_amount, f.ts))
            elif queue:
                sol_in, ts_in = queue.pop(0)
                closed.append(ClosedTrade(mint, ts_in, f.ts, sol_in, f.sol_amount))
        if queue:
            open_bags[mint] = sum(s for s, _ in queue)
    return closed, open_bags


def compute_metrics(trades: list[WalletTradeLike]) -> WalletMetrics:
    s = get_settings()
    m = WalletMetrics()
    if not trades:
        m.reasons.append("no_trades")
        return m

    closed, open_bags = build_closed_trades(trades)
    m.open_bags = len(open_bags)
    m.trades = len(closed)
    if not closed:
        m.reasons.append("no_closed_trades")
        return m

    pnls = [c.pnl for c in closed]
    m.wins = sum(1 for p in pnls if p > 0)
    m.win_rate = m.wins / len(closed)
    m.realized_pnl_sol = sum(pnls)
    m.avg_pnl_sol = m.realized_pnl_sol / len(closed)
    m.median_hold_min = statistics.median(c.hold_s for c in closed) / 60
    m.distinct_tokens = len({c.mint for c in closed})

    instant = sum(1 for c in closed if c.hold_s <= 60)
    m.instant_sell_ratio = instant / len(closed)

    per_token_pnl: dict[str, float] = {}
    for c in closed:
        per_token_pnl[c.mint] = per_token_pnl.get(c.mint, 0.0) + c.pnl
    tokens_with_2plus = {mint for mint, pnl in per_token_pnl.items() if pnl != 0}
    profitable = sum(1 for p in per_token_pnl.values() if p > 0)
    denom = max(1, len(per_token_pnl))
    m.profitable_token_ratio = profitable / denom
    _ = tokens_with_2plus

    sample_conf = min(1.0, m.trades / max(1, s.min_trades))
    pnl_norm = max(-1.0, min(1.0, m.avg_pnl_sol / 1.0))
    m.score = (
        0.35 * m.win_rate
        + 0.25 * (pnl_norm + 1) / 2
        + 0.20 * sample_conf
        + 0.20 * m.profitable_token_ratio
        - 0.30 * m.instant_sell_ratio
    )

    if m.trades < s.min_trades:
        m.reasons.append(f"low_sample:{m.trades}<{s.min_trades}")
    if m.win_rate < s.min_win_rate:
        m.reasons.append(f"win_rate:{m.win_rate:.2f}")
    if m.distinct_tokens < s.min_distinct_tokens:
        m.reasons.append(f"few_tokens:{m.distinct_tokens}")
    if m.instant_sell_ratio > s.max_instant_sell_ratio:
        m.reasons.append(f"instant_sell:{m.instant_sell_ratio:.2f}")
    if m.realized_pnl_sol < s.min_realized_pnl_sol:
        m.reasons.append(f"pnl:{m.realized_pnl_sol:.2f}")

    m.qualified = not m.reasons
    return m
