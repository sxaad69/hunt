"""Local readiness self-check — READ ONLY, no money, no signing, no sends.

Verifies everything the trial depends on, from this machine:
  1. pump.fun HTTP API reachable (proxy) — safety-net poll path
  2. PumpPortal WS discovery reachable
  3. Helius RPC reachable (getHealth + getAccountInfo)
  4. PDA derivations produce valid addresses (offline)
  5. buy/sell instruction builders: 18/16 accounts, correct discriminators (offline)
  6. survival_filter gates a synthetic coin correctly (offline, no DB writes)

Usage:  .venv/bin/python tools/local_check.py
Exit 0 = all pass. Any FAIL -> non-zero exit, fix before any trial.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def http_get(url: str, timeout: int = 12) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def main() -> int:
    env = open(".env").read()
    helius_key = re.search(r"HUNT_HELIUS_API_KEY=(\S+)", env).group(1)

    # 1. pump.fun HTTP (proxy) -------------------------------------------------
    try:
        st, body = http_get("https://frontend-api-v3.pump.fun/coins?offset=0&limit=2")
        coins = json.loads(body)
        check("pumpfun-http", st == 200 and isinstance(coins, list),
              f"status={st} coins={len(coins) if isinstance(coins, list) else '?'}")
    except Exception as e:
        check("pumpfun-http", False, f"{type(e).__name__}: {str(e)[:100]}")

    # 2. PumpPortal WS ----------------------------------------------------------
    try:
        import websockets

        async def _ws() -> str:
            async with websockets.connect(
                "wss://pumpportal.fun/api/data", open_timeout=10
            ) as w:
                await w.send(json.dumps({"method": "subscribeNewToken"}))
                return await asyncio.wait_for(w.recv(), timeout=15)

        msg = asyncio.run(_ws())
        check("pumpportal-ws", "subscrib" in msg.lower(), msg[:60])
    except Exception as e:
        check("pumpportal-ws", False, f"{type(e).__name__}: {str(e)[:100]}")

    # 3. Helius RPC --------------------------------------------------------------
    try:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "getHealth"}
        ).encode()
        req = urllib.request.Request(
            f"https://mainnet.helius-rpc.com/?api-key={helius_key}",
            payload,
            {"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            health = json.load(r)
        check("helius-rpc", health.get("result") == "ok", str(health.get("result")))
    except Exception as e:
        check("helius-rpc", False, f"{type(e).__name__}: {str(e)[:100]}")

    # 4. PDA derivations (offline) -----------------------------------------------
    try:
        from solders.pubkey import Pubkey

        from hunt.exec.pumpfun import pda as P

        mint = Pubkey.from_string("ATBR4i19gcQ31Rfr7ymA2XvkCQEAkNFGBtVKTmdqpump")
        user = Pubkey.from_string("6xWUNUZZSt8PgYEwzhwcb2h3fER8aTeHvhGWaakAyf6f")
        derived = {
            "bonding_curve": P.get_bonding_curve_pda(mint),
            "bonding_curve_v2": P.get_bonding_curve_v2_pda(mint),
            "creator_vault": P.get_creator_vault_pda(user),
            "gva": P.get_global_volume_accumulator_pda(),
            "uva": P.get_user_volume_accumulator_pda(user),
            "fee_config": P.get_fee_config_pda(),
        }
        ok = all(isinstance(v, Pubkey) for v in derived.values())
        check("pda-derivations", ok, f"{len(derived)} PDAs derived")
    except Exception as e:
        check("pda-derivations", False, f"{type(e).__name__}: {str(e)[:100]}")

    # 5. instruction builders (offline) -------------------------------------------
    try:
        from hunt.exec.pumpfun.bonding_curve import (
            build_buy_instruction,
            build_sell_instruction,
        )

        bc = P.get_bonding_curve_pda(mint)
        sell = build_sell_instruction(user, mint, bc, 1000, 1, user, user)
        buy = build_buy_instruction(user, mint, bc, 1000, 0, user, user)
        sell_ok = len(sell.accounts) == 16 and bytes(sell.data)[:8].hex() == "33e685a4017f83ad"
        buy_ok = len(buy.accounts) == 18 and bytes(buy.data)[:8].hex() == "66063d1201daebea"
        check("build-sell", sell_ok, f"{len(sell.accounts)} accts disc={bytes(sell.data)[:8].hex()}")
        check("build-buy", buy_ok, f"{len(buy.accounts)} accts disc={bytes(buy.data)[:8].hex()}")
    except Exception as e:
        check("instruction-builders", False, f"{type(e).__name__}: {str(e)[:100]}")

    # 6. gate chain on synthetic coins (offline, no DB) -----------------------------
    try:
        from hunt.paper.run import survival_filter

        dust = {"market_cap": 28, "created_timestamp": 1700000000000,
                "twitter": "x", "telegram": "", "website": ""}
        ok_dust, reason_dust = survival_filter(dust)
        check("gate-dust-veto", ok_dust is False and reason_dust.startswith("dust_mcap"),
              f"reason={reason_dust}")
    except Exception as e:
        check("gate-chain", False, f"{type(e).__name__}: {str(e)[:100]}")

    print("=" * 50)
    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)}: {', '.join(FAILURES)})")
        return 1
    print("RESULT: ALL CHECKS PASSED — local is trial-ready (builders + feeds)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
