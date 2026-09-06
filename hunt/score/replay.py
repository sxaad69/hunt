from __future__ import annotations

import httpx
from loguru import logger

from hunt.config import get_settings
from hunt.utils.http import post_json_rpc
from hunt.utils.ratelimit import RateLimiter
from hunt.utils.swaps import (
    WalletTradeLike,
    deltas_from_ws_notification,
    extract_swap_for_owner,
)

DAILY_CALL_BUDGET = 15000


class ReplayEngine:
    def __init__(self, client: httpx.AsyncClient, db) -> None:
        s = get_settings()
        self.s = s
        self.client = client
        self.db = db
        self.limiter = RateLimiter(rate_per_sec=3.0, burst=8)
        self._budget_exhausted = False

    async def _budget_ok(self) -> bool:
        if self._budget_exhausted:
            return False
        used = await self.db.kv_get_int(f"helius_replay_calls:{_today()}")
        if used >= DAILY_CALL_BUDGET:
            self._budget_exhausted = True
            logger.warning("daily replay call budget exhausted ({})", DAILY_CALL_BUDGET)
            return False
        return True

    async def _rpc(self, method: str, params: list) -> dict | list | None:
        if not await self._budget_ok():
            return None
        await self.limiter.acquire()
        resp = await post_json_rpc(self.client, self.s.rpc_http, method, params)
        await self.db.kv_bump_daily("helius_replay_calls")
        return resp

    async def fetch_history(self, owner: str, max_txs: int | None = None) -> list[WalletTradeLike]:
        max_txs = max_txs or self.s.replay_max_txs_per_wallet
        resp = await self._rpc(
            "getSignaturesForAddress",
            [owner, {"limit": min(max_txs * 2, 1000)}],
        )
        if not resp or "result" not in resp:
            logger.debug("signatures fetch failed {}", owner[:8])
            return []
        entries = [
            e for e in (resp["result"] or [])
            if not e.get("err") and e.get("signature")
        ]

        trades: list[WalletTradeLike] = []
        seen: set[str] = set()
        for entry in entries:
            if len(trades) >= max_txs:
                break
            sig = entry["signature"]
            if sig in seen:
                continue
            seen.add(sig)
            tx_resp = await self._rpc(
                "getTransaction",
                [
                    sig,
                    {
                        "encoding": "jsonParsed",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            )
            if not tx_resp or not tx_resp.get("result"):
                continue
            r = tx_resp["result"]
            meta = r.get("meta") or {}
            if meta.get("err"):
                continue
            ts = int(r.get("blockTime") or entry.get("blockTime") or 0)
            deltas = deltas_from_ws_notification({
                "transaction": {"transaction": r.get("transaction"), "meta": meta}
            })
            for s in extract_swap_for_owner(deltas, owner):
                trades.append(WalletTradeLike(sig, ts, s.mint, s.side, s.sol_amount, s.token_amount))

        return trades[:max_txs]


def _today() -> str:
    import time as _t

    return _t.strftime("%Y-%m-%d")
