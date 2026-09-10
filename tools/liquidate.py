"""Emergency liquidation: sell EVERY nonzero token bag to SOL.

AWS-ONLY operator tool (the wallet key lives on AWS; the Mac runs paper only).
Run on AWS via the git-only flow — never scp scripts to /tmp:

    sudo -u hunt .venv/bin/python tools/liquidate.py --confirm

Sells full remainders with close_ata=True (SPL CloseAccount reverts on partial
balances, so only full-remainder sells may close the ATA). Failed sells keep
the position/token — nothing is ever phantom-closed.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from hunt.exec.live import LiveExecutor

TOKEN_PROGRAMS = [
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
]


async def _bags(ex: LiveExecutor) -> list[tuple[str, int, int]]:
    out = []
    for prog in TOKEN_PROGRAMS:
        resp = await ex._raw_rpc(
            "getTokenAccountsByOwner",
            [ex.wallet, {"programId": prog}, {"encoding": "jsonParsed"}],
        )
        for a in resp.get("value", []):
            info = a["account"]["data"]["parsed"]["info"]
            amt = info["tokenAmount"]
            if amt["amount"] == "0":
                continue
            out.append((info["mint"], int(amt["amount"]), amt["decimals"]))
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description="Sell all token bags to SOL.")
    ap.add_argument("--confirm", action="store_true",
                    help="required: acknowledge this sells everything")
    args = ap.parse_args()
    if not args.confirm:
        print("REFUSING: pass --confirm to actually sell. Dry listing bags:")
    ex = LiveExecutor()
    print("wallet:", ex.wallet)
    bags = await _bags(ex)
    print("bags found:", len(bags))
    if not args.confirm:
        for mint, raw, dec in bags:
            print(f"  {mint[:16]}  {raw / (10 ** dec):,.4f} tokens")
        return 0
    total_sol = 0.0
    for mint, raw, dec in bags:
        print(f"--- selling {mint[:16]}  {raw / (10 ** dec):,.4f} tokens ({raw} raw)")
        r = await ex.sell(mint, raw, close_ata=True)
        if r and r.ok:
            total_sol += r.sol_lamports / 1e9
            print(f"   SOLD venue={r.venue} got={r.sol_lamports / 1e9:.6f} SOL sig={r.signature[:20]}..")
            await asyncio.sleep(3)
        else:
            print("   SELL FAILED (tokens kept)")
    print("=" * 50)
    print(f"RECEIVED FROM SELLS: {total_sol:.6f} SOL")
    print(f"FINAL FREE SOL BALANCE: {await ex.balance_sol():.6f} SOL")
    left = await _bags(ex)
    for mint, raw, dec in left:
        print("LEFTOVER:", mint, raw)
    print("leftover bags:", len(left))
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
