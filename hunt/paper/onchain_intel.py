"""On-chain holder concentration for the entry gate.

`advanced-indexer.pump.fun/in-memory-coin` returned top10=0 / snipers=0 /
dev_pct=0 for every species-A ACCEPT in the first live window (Rufus class)
while getTokenLargestAccounts showed 98–100% of circulating supply in the
top wallets. This module is the ground-truth replacement: it NEVER treats
RPC failure or an empty result as "clean".

Concentration is measured on CIRCULATING supply only (total supply minus the
associated bonding-curve token account). Including the curve would make
every species-A coin look 90%+ concentrated.
"""
from __future__ import annotations

from typing import Iterable, Optional

from solders.pubkey import Pubkey

from hunt.exec.pumpfun.constants import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from hunt.exec.pumpfun.pda import get_associated_token_address, get_bonding_curve_pda

# Match survival_filter top10_heavy (>75).
TOP10_VETO_PCT = 75.0


def protocol_token_accounts(mint: str) -> set[str]:
    """Bonding-curve ATAs (legacy SPL + Token-2022). Not 'holders'."""
    mint_pk = Pubkey.from_string(mint)
    curve = get_bonding_curve_pda(mint_pk)
    out = set()
    for tp in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        out.add(str(get_associated_token_address(curve, mint_pk, tp)))
    return out


def circulating_top10_pct(
    supply_raw: int,
    accounts: Iterable[dict],
    exclude: set[str],
) -> Optional[float]:
    """Return top-10 % of circulating raw supply, or None if unmeasurable.

    `accounts` items: {"address": str, "amount": int} (raw units).
    None = we do not have a number — callers must FAIL CLOSED on LIVE,
    never coerce to 0 (the Rufus false-clean).
    """
    if supply_raw <= 0:
        return None
    curve_raw = 0
    holders: list[int] = []
    for acc in accounts:
        addr = str(acc.get("address") or "")
        amt = int(acc.get("amount") or 0)
        if addr in exclude:
            curve_raw += amt
            continue
        if amt > 0:
            holders.append(amt)
    held = supply_raw - curve_raw
    if held <= 0 or not holders:
        return None
    holders.sort(reverse=True)
    top = sum(holders[:10])
    return 100.0 * top / held


def parse_largest_accounts(rpc_result: dict | None) -> list[dict]:
    """Flatten getTokenLargestAccounts JSON-RPC result into {address, amount}."""
    if not rpc_result:
        return []
    value = rpc_result.get("value") if isinstance(rpc_result, dict) else None
    if isinstance(value, dict):
        value = value.get("value")
    rows = value if isinstance(value, list) else []
    out = []
    for row in rows:
        addr = row.get("address")
        inner = row.get("uiTokenAmount") or {}
        ta = row.get("amount") if row.get("amount") is not None else inner.get("amount")
        if addr is None or ta is None:
            continue
        try:
            out.append({"address": str(addr), "amount": int(ta)})
        except (TypeError, ValueError):
            continue
    return out


def parse_supply_raw(rpc_result: dict | None) -> Optional[int]:
    if not rpc_result:
        return None
    try:
        return int(rpc_result["value"]["amount"])
    except (TypeError, KeyError, ValueError):
        return None


VS0_SOL = 30.0
GRAD_REAL_SOL = 85.0


def curve_fdv_sol(
    virtual_sol_reserves: int,
    virtual_token_reserves: int,
    token_total_supply: int,
) -> Optional[float]:
    """On-chain fully-diluted mcap in SOL. Decimals cancel (raw/raw).

    Birth curve (~30 SOL virtual, ~1.073e9 UI virtual tokens, 1e9 supply) → ~28 SOL,
    which is the number the dust floor was calibrated on. None if drained/unreadable.
    """
    if virtual_sol_reserves <= 0 or virtual_token_reserves <= 0 or token_total_supply <= 0:
        return None
    return (virtual_sol_reserves / 1e9) * (token_total_supply / virtual_token_reserves)


def curve_fill_pct(
    real_sol_reserves: int | None = None,
    virtual_sol_reserves: int | None = None,
) -> Optional[float]:
    """Bonding-curve fill 0–100. Prefer real SOL / 85; else virtual − 30 SOL."""
    real_sol: float | None = None
    if real_sol_reserves is not None:
        real_sol = max(0.0, real_sol_reserves / 1e9)
    elif virtual_sol_reserves is not None:
        real_sol = max(0.0, virtual_sol_reserves / 1e9 - VS0_SOL)
    else:
        return None
    return max(0.0, min(100.0, 100.0 * real_sol / GRAD_REAL_SOL))


async def fetch_curve_mcap(
    http_client, rpc_http: str, mint: str
) -> tuple[Optional[float], bool, Optional[float]]:
    """Return (fdv_sol, graduated, fill_pct). fdv is None when unreadable or complete."""
    from hunt.exec.pumpfun.bonding_curve import fetch_bonding_curve_state
    try:
        st = await fetch_bonding_curve_state(rpc_http, mint, http_client=http_client)
    except Exception:
        return None, False, None
    fill = curve_fill_pct(st.real_sol_reserves, st.virtual_sol_reserves)
    if st.complete:
        return None, True, fill
    return curve_fdv_sol(
        st.virtual_sol_reserves, st.virtual_token_reserves, st.token_total_supply
    ), False, fill


async def fetch_amm_mcap(http_client, rpc_http: str, pool_address: str) -> Optional[float]:
    """PumpSwap vault FDV in SOL. None if unreadable or inverted pool."""
    from hunt.exec.pumpfun.pumpswap import fetch_pool_state
    from hunt.watch.price_feed import amm_price_sol
    try:
        st = await fetch_pool_state(rpc_http, pool_address, http_client=http_client)
    except Exception:
        return None
    if st.base_is_sol:
        return None

    async def bal(addr: str) -> int:
        try:
            r = await http_client.post(
                rpc_http,
                json={"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountBalance",
                      "params": [addr]},
                timeout=10,
            )
            return int((((r.json() or {}).get("result") or {}).get("value") or {}).get("amount") or 0)
        except Exception:
            return 0

    parsed = amm_price_sol(
        await bal(str(st.pool_base_token_account)),
        await bal(str(st.pool_quote_token_account)),
    )
    return parsed[1] if parsed else None


async def fetch_onchain_top10(http_client, rpc_http: str, mint: str) -> Optional[float]:
    """LIVE-grade top-10% of circulating supply, or None on any failure."""
    async def rpc(method: str, params: list) -> dict | None:
        try:
            r = await http_client.post(
                rpc_http,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=10,
            )
            j = r.json()
            return j.get("result") if j.get("error") is None else None
        except Exception:
            return None

    supply = parse_supply_raw(await rpc("getTokenSupply", [mint]))
    largest = parse_largest_accounts(
        await rpc("getTokenLargestAccounts", [mint])
    )
    pct = circulating_top10_pct(supply_raw=supply or 0, accounts=largest,
                                 exclude=protocol_token_accounts(mint))
    return pct
