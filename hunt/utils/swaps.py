from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hunt.config import QUOTE_MINTS, USDC, WSOL


@dataclass
class ParsedSwap:
    owner: str
    side: str
    mint: str
    token_amount: float
    sol_amount: float
    quote_mint: str = WSOL


@dataclass
class AccountDeltas:
    native_delta_lamports: dict[str, int] = field(default_factory=dict)
    token_deltas: dict[tuple[str, str], float] = field(default_factory=dict)


def deltas_from_ws_notification(notification: dict[str, Any]) -> AccountDeltas:
    result = notification["transaction"]
    meta = result.get("meta") or {}
    tx = result.get("transaction") or {}
    account_keys: list[str] = []
    msg = tx.get("message") or {}
    if isinstance(msg.get("accountKeys"), list):
        for k in msg["accountKeys"]:
            if isinstance(k, dict):
                account_keys.append(k.get("pubkey", ""))
            else:
                account_keys.append(str(k))
    loaded = meta.get("loadedAddresses") or {}
    for k in (loaded.get("writable") or []) + (loaded.get("readonly") or []):
        if k not in account_keys:
            account_keys.append(k)

    d = AccountDeltas()
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    fee = meta.get("fee") or 0
    for i in range(min(len(pre), len(post))):
        delta = post[i] - pre[i]
        addr = account_keys[i] if i < len(account_keys) else f"idx{i}"
        d.native_delta_lamports[addr] = delta - (fee if i == 0 else 0)

    pre_tb = {(t.get("accountIndex"), t.get("mint")): t for t in (meta.get("preTokenBalances") or [])}
    post_tb = {(t.get("accountIndex"), t.get("mint")): t for t in (meta.get("postTokenBalances") or [])}
    for idx_mint in set(pre_tb) | set(post_tb):
        p = pre_tb.get(idx_mint)
        q = post_tb.get(idx_mint)
        entry_owner = (q or p).get("owner")
        if not entry_owner:
            continue
        pa = float((p or {}).get("uiTokenAmount", {}).get("uiAmount") or 0)
        qa = float((q or {}).get("uiTokenAmount", {}).get("uiAmount") or 0)
        delta = qa - pa
        if delta != 0:
            key = (entry_owner, idx_mint[1])
            d.token_deltas[key] = d.token_deltas.get(key, 0.0) + delta

    return d


def extract_swap_for_owner(deltas: AccountDeltas, owner: str) -> list[ParsedSwap]:
    swaps: list[ParsedSwap] = []
    owner_tokens = {
        mint: raw
        for (o, mint), raw in deltas.token_deltas.items()
        if o == owner and raw != 0
    }
    wsol_change = owner_tokens.pop(WSOL, 0.0)
    usdc_change = owner_tokens.pop(USDC, 0.0)
    quote_used = WSOL if abs(wsol_change) >= abs(usdc_change) else USDC
    q_change = wsol_change if quote_used == WSOL else usdc_change

    for mint, token_change in owner_tokens.items():
        if token_change > 1e-12 and q_change < -1e-12:
            swaps.append(ParsedSwap(owner, "BUY", mint, token_change, -q_change, quote_used))
        elif token_change < -1e-12 and q_change > 1e-12:
            swaps.append(ParsedSwap(owner, "SELL", mint, -token_change, q_change, quote_used))
    return swaps


@dataclass
class WalletTradeLike:
    signature: str
    ts: int
    mint: str
    side: str
    sol_amount: float
    token_amount: float


def extract_swaps_from_helius_history(item: dict[str, Any], owner: str) -> list[WalletTradeLike]:
    out: list[WalletTradeLike] = []
    ts = int(item.get("timestamp") or item.get("blockTime") or 0)
    sig = item.get("signature") or ""
    token_transfers = item.get("tokenTransfers") or []
    native_transfers = item.get("nativeTransfers") or []

    tok: dict[str, float] = {}
    quote_delta = 0.0
    for t in token_transfers:
        mint = t.get("mint")
        amt = float(t.get("tokenAmount") or 0)
        frm, to = t.get("fromUserAccount"), t.get("toUserAccount")
        if not mint:
            continue
        if mint in QUOTE_MINTS:
            if frm == owner:
                quote_delta -= amt
            if to == owner:
                quote_delta += amt
            continue
        if frm == owner:
            tok[mint] = tok.get(mint, 0.0) - amt
        if to == owner:
            tok[mint] = tok.get(mint, 0.0) + amt

    native_delta = sum(
        (n.get("lamports") or 0) / 1e9
        for n in native_transfers
        if n.get("fromUserAccount") == owner or n.get("toUserAccount") == owner
    )
    sol_delta = quote_delta + native_delta

    for mint, change in tok.items():
        if change > 1e-12 and sol_delta < -1e-12:
            out.append(WalletTradeLike(sig, ts, mint, "BUY", -sol_delta, change))
        elif change < -1e-12 and sol_delta > 1e-12:
            out.append(WalletTradeLike(sig, ts, mint, "SELL", sol_delta, -change))
    return out
