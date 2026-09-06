from __future__ import annotations

import argparse
from typing import Optional

import base58
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from hunt.config import WSOL, get_settings


def load_keypair() -> Optional[Keypair]:
    s = get_settings()
    if not s.wallet_private_key:
        return None
    return Keypair.from_base58_string(s.wallet_private_key)


def wallet_address(kp: Keypair) -> str:
    return str(kp.pubkey())


def gen_wallet() -> None:
    kp = Keypair()
    print("address     :", str(kp.pubkey()))
    print("private key :", base58.b58encode(bytes(kp)).decode())
    print()
    print("Store the private key in .env as HUNT_WALLET_PRIVATE_KEY.")
    print("Never reuse your main wallet. Never commit this file.")


async def balance() -> None:
    from solana.rpc.async_api import AsyncClient

    kp = load_keypair()
    if not kp:
        print("HUNT_WALLET_PRIVATE_KEY not set")
        return
    s = get_settings()
    async with AsyncClient(s.rpc_http) as client:
        resp = await client.get_balance(kp.pubkey())
        lamports = resp.value
        print(f"{wallet_address(kp)}: {lamports / 1e9:.6f} SOL (raw {lamports} lamports)")
        print(f"WSOL mint for reference: {WSOL}")


def main() -> None:
    p = argparse.ArgumentParser(prog="hunt.utils.solana")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen-wallet")
    sub.add_parser("balance")
    args = p.parse_args()
    if args.cmd == "gen-wallet":
        gen_wallet()
    elif args.cmd == "balance":
        import asyncio

        asyncio.run(balance())


if __name__ == "__main__":
    main()
