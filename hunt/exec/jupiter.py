from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Optional

import httpx
from loguru import logger
from solders.keypair import Keypair

from hunt.config import get_settings
from hunt.utils.http import fetch_json


@dataclass
class QuoteResult:
    in_mint: str
    out_mint: str
    in_amount_raw: int
    out_amount_raw: int
    raw: dict


class JupiterClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.s = get_settings()
        self.client = client

    def _headers(self) -> dict:
        h = {"accept": "application/json"}
        if self.s.jup_api_key:
            h["x-api-key"] = self.s.jup_api_key
        return h

    async def quote(
        self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int | None = None
    ) -> Optional[QuoteResult]:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "slippageBps": str(slippage_bps or self.s.slippage_bps),
        }
        data = await fetch_json(
            self.client, "GET", f"{self.s.jup_base}/quote",
            params=params, headers=self._headers(),
        )
        if not data or "inAmount" not in data:
            logger.debug("jup quote failed {}->{}", input_mint[:8], output_mint[:8])
            return None
        return QuoteResult(
            in_mint=input_mint,
            out_mint=output_mint,
            in_amount_raw=int(data["inAmount"]),
            out_amount_raw=int(data["outAmount"]),
            raw=data,
        )

    async def build_swap_transaction(self, quote: QuoteResult, user_pubkey: str) -> Optional[str]:
        body = {
            "quoteResponse": quote.raw,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "priorityLevel": "veryHigh",
                    "maxLamports": self.s.priority_fee_max_lamports,
                }
            },
        }
        data = await fetch_json(
            self.client, "POST", f"{self.s.jup_base}/swap",
            json_body=body, headers=self._headers(),
        )
        if not data or not data.get("swapTransaction"):
            logger.warning("jup swap build failed")
            return None
        return data["swapTransaction"]

    async def sign_and_send(self, swap_tx_b64: str, kp: Keypair) -> Optional[str]:
        from solana.rpc.async_api import AsyncClient
        from solders.transaction import VersionedTransaction

        try:
            raw = base64.b64decode(swap_tx_b64)
            unsigned = VersionedTransaction.from_bytes(raw)
            signed = VersionedTransaction(unsigned.message, [kp])
            async with AsyncClient(self.s.rpc_http) as rpc:
                resp = await rpc.send_raw_transaction(bytes(signed))
                sig = str(resp.value)
                await rpc.confirm_transaction(resp.value)
                return sig
        except Exception as e:
            logger.error("send failed: {}", e)
            return None
