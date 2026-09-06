from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hunt.config import WSOL
from hunt.utils.swaps import deltas_from_ws_notification, extract_swap_for_owner


@dataclass
class TradeSignal:
    wallet: str
    mint: str
    side: str
    sol_amount: float
    token_amount: float
    signature: str
    slot: int


def parse_notification(result: dict[str, Any], watched_wallets: set[str]) -> list[TradeSignal]:
    tx = result.get("transaction") or {}
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []
    sig = result.get("signature") or ""
    slot = int(result.get("slot") or 0)

    deltas = deltas_from_ws_notification({"transaction": result})
    signals: list[TradeSignal] = []
    for owner in watched_wallets:
        swaps = extract_swap_for_owner(deltas, owner)
        for s in swaps:
            if s.mint == WSOL:
                continue
            signals.append(
                TradeSignal(
                    wallet=owner,
                    mint=s.mint,
                    side=s.side,
                    sol_amount=s.sol_amount,
                    token_amount=s.token_amount,
                    signature=sig,
                    slot=slot,
                )
            )
    return signals


def dedupe(signals: list[TradeSignal], seen: set[str], max_size: int = 5000) -> list[TradeSignal]:
    out = []
    for s in signals:
        key = f"{s.signature}:{s.wallet}:{s.mint}:{s.side}"
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    if len(seen) > max_size:
        seen.clear()
    return out
