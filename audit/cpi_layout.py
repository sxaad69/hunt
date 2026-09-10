#!/usr/bin/env python3
"""Capture ground-truth PumpFun buy CPI layout for current-generation coins.

Phase-0 ground truth for the execution-bridge repair (2026-09-10).

WHY: buys of curve-stage Token-2022 coins fail with `UnsupportedQuoteMint`
(6063) even though the ATA creation succeeds. The vendored 18-account buy
builder was captured from a LEGACY coin (mmrich trial). Current coins use a
different quote mint / curve layout, so the pump program rejects our buy.

WHAT THIS SCRIPT DOES (read-only, no writes, no live trading):
  1. Read the on-chain bonding-curve account for a list of mints and dump:
       - account length (legacy 124 vs 151-byte new layout)
       - complete flag, creator
       - is_cashback_coin / is_mayhem_mode flags
       - quote-mint bytes (new layout, byte ~83..115) hex
  2. If HELIUS api key is available and (optional) a live tx signature is
     given, decode that transaction's inner CPI into the pump program and
     print the exact account list + instruction data so we can diff it
     against build_buy_instruction.

Usage (AWS preferred for network parity, but reads fine anywhere with an RPC):
  sudo -u hunt .venv/bin/python audit/cpi_layout.py --mint MINT [--mint MINT...]
  sudo -u hunt .venv/bin/python audit/cpi_layout.py --mint MINT --txhex HEX

No secrets are printed; only account program IDs and known program constants.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import struct
import sys

import httpx

from solders.pubkey import Pubkey

from hunt.exec.pumpfun.constants import (
    PUMP_FUN_PROGRAM,
    SOL_MINT,
)
from hunt.exec.pumpfun.pda import get_bonding_curve_pda
from hunt.config import get_settings

# Known quote mints to annotate.
KNOWN_MINTS = {
    "So11111111111111111111111111111111111111112": "WSOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KjNkENNmaJYEG7nQXdPu": "USDT",
}


def rpc(rpc_url: str, method: str, params: list) -> dict:
    """Blocking RPC call."""
    resp = httpx.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    resp.raise_for_status()
    return resp.json().get("result", {})


def probe_curve(rpc_url: str, mint: str) -> None:
    print(f"\n=== {mint} ===")
    curve = get_bonding_curve_pda(Pubkey.from_string(mint))
    print(f"  bonding_curve (derived):  {curve}")

    result = rpc(rpc_url, "getAccountInfo", [str(curve), {"encoding": "base64"}])
    value = result.get("value")
    if not value:
        print("  ! curve account NOT FOUND (deleted/never created)")
        return

    raw = value.get("data", [])
    data = base64.b64decode(raw[0]) if isinstance(raw, list) and raw else b""
    print(f"  curve account owner:      {value.get('owner')}")
    print(f"  curve data length:        {len(data)} bytes   ({'NEW 151-byte' if len(data) >= 151 else 'LEGACY 124-byte' if len(data) == 124 else 'unknown'})")

    if len(data) < 81:
        print("  ! too short to parse")
        return

    vteam = struct.unpack_from("<Q", data, 8)[0]
    vsol = struct.unpack_from("<Q", data, 16)[0]
    complete = data[48] != 0
    creator = data[49:81]
    creator = creator.hex()
    print(f"  virtual_token_reserves:   {vteam}")
    print(f"  virtual_sol_reserves:     {vsol}")
    print(f"  complete:                 {complete}")
    print(f"  creator:                  {creator}")

    if len(data) >= 82:
        mayhem = data[81] != 0
        print(f"  mayhem_mode (byte81):     {mayhem}")
    if len(data) >= 83:
        cashback = data[82] != 0
        print(f"  cashback_coin (byte82):   {cashback}")

    # new layout: quote mint likely at bytes 83..115 (32 bytes)
    if len(data) >= 115:
        qm = data[83:115]
        # A pubkey won't be all zeros; if it's non-trivial, show it.
        if qm != b"\x00" * 32:
            # try to resolve to an address
            import base58
            qm_addr = ""
            try:
                qm_addr = base58.b58encode(qm).decode()
            except Exception:
                pass
            tag = KNOWN_MINTS.get(qm_addr, "UNKNOWN")
            print(f"  quote_mint bytes[83:115]: {qm.hex()}  ({tag})")
            if tag == "UNKNOWN":
                print(f"  !! UNKNOWN quote mint! This is the cause of UnsupportedQuoteMint (6063).")
        else:
            print(f"  quote_mint bytes[83:115]: zeros (no explicit quote mint)")


def decode_tx(rpc_url: str, txhex: str, mint: str) -> None:
    """Best-effort decode of a raw transaction hex looking for pump CPI."""
    print(f"\n=== tx decode for {mint} ===")
    try:
        raw = bytes.fromhex(txhex)
    except (binascii.Error, ValueError):
        print("  ! --txhex must be a hex string")
        return
    print(f"  raw tx len: {len(raw)} bytes")
    try:
        # uses solders for the full message layout
        from solders.transaction import VersionedTransaction
        tx = VersionedTransaction.from_bytes(raw)
        msg = tx.message
        # accounts in the message
        accts = msg.account_keys
        print(f"  num account keys: {len(accts)}")
        for i, a in enumerate(accts):
            print(f"    [{i}] {a}")
    except Exception as e:
        print(f"  ! solders decode failed: {e}. Falling back to raw hex dump slice.")
        print(f"  falling back to json: {txhex[:128]}...")


def fetch_recent_tx(rpc_url: str, mint: str, account: str, slot_cutoff: int = 0) -> None:
    """Pull recent transaction signatures for a mint/curve address and print the
    account list of any that touches the pump program (with full CPI accounts)."""
    import base64 as b64mod
    print(f"\n=== recent tx for {mint[:8]} via {account} ===")
    sigs = rpc(rpc_url, "getSignaturesForAddress", [account, {"limit": 10}])
    logs = sigs.get("result") if isinstance(sigs, dict) else sigs
    if not isinstance(logs, list) or not logs:
        print("  no recent signatures")
        return
    print(f"  found {len(logs)} signatures")
    for sig in logs:
        ts = sig.get("blockTime") or 0
        if slot_cutoff and ts < slot_cutoff:
            continue
        s = sig.get("signature", "")
        try:
            tx = rpc(rpc_url, "getTransaction", [s, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        except Exception as e:
            print(f"    ! getTransaction {s[:12]} failed: {e}")
            continue
        txres = tx.get("result") if isinstance(tx, dict) else tx
        if not txres:
            continue
        meta = txres.get("meta") or {}
        if meta.get("err"):
            continue
        # find pump program account index in the resolved address table
        inner = meta.get("innerInstructions") or []
        msg = txres.get("message") or {}
        accts = msg.get("accountKeys") or []
        if not accts or not isinstance(accts[0], dict):
            print(f"    {s[:12]} no parsed account keys (using unit list)")
            continue
        idx_of = {}
        for i, a in enumerate(accts):
            pk = a.get("pubkey", "")
            idx_of[pk] = i
        pump_idx = idx_of.get(str(PUMP_FUN_PROGRAM))
        if pump_idx is None:
            continue
        # outer instruction that targets pump program
        for inx in inner:
            insts = inx.get("instructions", [])
            for inst in insts:
                p = inst.get("programId", "")
                if p == str(PUMP_FUN_PROGRAM):
                    print(f"\n  PUMB BUY CPI in {s[:16]}:")
                    print(f"    programId: {p}")
                    print(f"    data: {inst.get('data','')[:120]}")
                    for a in inst.get("accounts", []):
                        print(f"      - {a}")
                    return
        print(f"    {s[:12]} no pump-program outer CPI")
    print("  no pump program CPI found in recent txs")


BUY_DISC = bytes([102, 6, 61, 18, 1, 218, 235, 234])
SELL_DISC = bytes([51, 230, 133, 164, 1, 127, 131, 173])


def _resolve_accounts(inst: dict, keys: list[str]) -> list[str]:
    """Resolve an instruction's account list to pubkey strings.

    innerInstructions from jsonParsed can hand us pubkey strings OR absolute
    indexes into the tx account-keys list. Normalize to strings."""
    out = []
    for a in inst.get("accounts") or []:
        if isinstance(a, dict):
            a = a.get("pubkey", "")
        if isinstance(a, int):
            a = keys[a] if a < len(keys) else f"?idx{a}"
        out.append(str(a))
    return out


def capture_cpi(rpc_url: str, mint: str, limit: int = 40) -> None:
    """Capture the REAL buy/sell CPI into the pump program for a mint.

    Robust variant of fetch_recent_tx: parses BOTH the raw base64 tx (to get
    the authoritative account-keys list) and the jsonParsed inner instructions,
    then scans every instruction (top-level AND inner) that targets the pump
    program, resolving each account slot. Returns on the first BUY found.
    """
    import base64 as b64mod
    from solders.transaction import VersionedTransaction

    print(f"\n=== capture REAL pump CPI for {mint} ===")
    curve = get_bonding_curve_pda(Pubkey.from_string(mint))
    sigs = rpc(rpc_url, "getSignaturesForAddress", [str(curve), {"limit": limit}])
    logs = sigs.get("result") if isinstance(sigs, dict) else sigs
    if not isinstance(logs, list) or not logs:
        print("  no recent signatures")
        return

    ok_buy = 0
    ok_sell = 0
    for sig in sorted(logs, key=lambda s: s.get("blockTime") or 0, reverse=True):
        s = sig.get("signature", "")
        # raw base64 first -> authoritative account-keys list
        try:
            raw_res = rpc(rpc_url, "getTransaction", [s, {"encoding": "base64", "maxSupportedTransactionVersion": 0}])
        except Exception as e:
            print(f"  ! getTransaction(raw) {s[:12]} failed: {e}")
            continue
        raw_tx = raw_res.get("result") if isinstance(raw_res, dict) else raw_res
        if not raw_tx:
            continue
        meta = raw_tx.get("meta") or {}
        if meta.get("err"):
            continue
        try:
            b64 = raw_tx["transaction"][0]
            vt = VersionedTransaction.from_bytes(b64mod.b64decode(b64))
            keys = [str(k) for k in vt.message.account_keys]
        except Exception as e:
            print(f"  ! solders parse {s[:12]} failed: {e}")
            continue

        # jsonParsed for inner instructions (programId/accounts)
        try:
            parsed_res = rpc(rpc_url, "getTransaction", [s, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "transactionDetails": "full"}])
        except Exception as e:
            parsed_res = {}
        parsed = parsed_res.get("result") if isinstance(parsed_res, dict) else parsed_res
        if not parsed:
            continue
        pmeta = parsed.get("meta") or {}
        pmsg = parsed.get("message") or {}

        def try_inst(inst: dict, where: str) -> str:
            nonlocal ok_buy, ok_sell
            pid = inst.get("programId", "")
            pmatch = pid == str(PUMP_FUN_PROGRAM)
            data_b58 = inst.get("data", "")
            if pmatch or data_b58:
                # decode base58 data to find discriminator
                dbytes = None
                if data_b58:
                    import base58 as b58
                    try:
                        dbytes = b58.b58decode(data_b58)
                    except Exception:
                        dbytes = None
                disc = dbytes[:8] if dbytes and len(dbytes) >= 8 else b""
                is_buy = disc == BUY_DISC
                is_sell = disc == SELL_DISC
                if pmatch or is_buy or is_sell:
                    accts = _resolve_accounts(inst, keys)
                    if is_buy:
                        ok_buy += 1
                        print(f"\n  >>> BUY CPI {s[:16]} [{where}] accounts={len(accts)} bytes={len(dbytes) if dbytes else 0}")
                        _print_accts(accts, keys)
                        return "buy"
                    if is_sell:
                        ok_sell += 1
                        if ok_sell <= 1:
                            print(f"\n  sell CPI {s[:16]} [{where}] accounts={len(accts)} bytes={len(dbytes) if dbytes else 0}")
                            _print_accts(accts, keys)
                        return "sell"
            return ""

        # top-level instructions first (usually a router -> not pump directly)
        for inst in pmsg.get("instructions") or []:
            if try_inst(inst, "top") == "buy":
                return
        # inner instructions (the pump CPI lives here for router buys)
        for inx in pmeta.get("innerInstructions") or []:
            for inst in inx.get("instructions") or []:
                r = try_inst(inst, f"inner@{inx.get('index')}")
                if r == "buy":
                    return
        if ok_buy > 0:
            return

    print("  no BUY pump CPI found in recent txs")


def _print_accts(accts: list[str], keys: list[str]) -> None:
    KNOWN = {
        "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf": "global",
        "GesfTA3X2arioaHp8bbKdjG9vJtskViWACZoYvxp4twS": "fee_recipient",
        "11111111111111111111111111111111": "system_program",
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "TOKEN_PROGRAM",
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "TOKEN_2022",
        "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1": "event_authority",
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "PUMP_PROGRAM",
        "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ": "FEE_PROGRAM",
        "So11111111111111111111111111111111111111112": "WSOL",
    }
    for i, a in enumerate(accts):
        tag = KNOWN.get(a, "")
        print(f"    [{i:>2}] {a}   {tag}")


def probe_curve_quote_only(rpc_url: str, mint: str) -> None:
    curve = get_bonding_curve_pda(Pubkey.from_string(mint))
    result = rpc(rpc_url, "getAccountInfo", [str(curve), {"encoding": "base64"}])
    value = result.get("value")
    if not value:
        print(f"  {mint[:14]}: (no curve account)")
        return
    raw = value.get("data", [])
    data = base64.b64decode(raw[0]) if isinstance(raw, list) and raw else b""
    if len(data) >= 115:
        qm = data[83:115]
        qm_addr = ""
        if qm != b"\x00" * 32:
            import base58
            try:
                qm_addr = base58.b58encode(qm).decode()
            except Exception:
                pass
            tag = KNOWN_MINTS.get(qm_addr, f"TOKEN-2022:{str(Pubkey(bytes(qm)))[:8]}")
        else:
            tag = "WSOL"
        print(f"  {mint[:14]}: len={len(data)} quote_mint={tag}")


def scan_db(rpc_url: str, db_path: str, hours: int, limit: int) -> None:
    """Classify quote-mint of recent ACCEPT-ish decisions from a decisions DB."""
    import time
    import sqlite3

    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    cut = time.time() - hours * 3600
    rows = c.execute(
        """SELECT mint, symbol, market_cap, decision FROM paper_decisions
           WHERE decided_at >= ? AND decision IN ('ACCEPT','PAPER','LIVE')
           ORDER BY decided_at DESC LIMIT ?""",
        (cut, limit),
    ).fetchall()
    c.close()
    print(f"scan {len(rows)} decisions from {db_path.split('/')[-1]} "
          f"({hours}h, <=mcap). quote-mint distribution:")
    counts: dict[str, int] = {}
    exotic: list[tuple] = []
    unique_quotes: dict[str, int] = {}
    for r in rows:
        curve = get_bonding_curve_pda(Pubkey.from_string(r["mint"]))
        try:
            result = rpc(rpc_url, "getAccountInfo", [str(curve), {"encoding": "base64"}])
            value = result.get("value") if isinstance(result, dict) else None
            if not value:
                counts["GONE"] = counts.get("GONE", 0) + 1
                continue
            data = base64.b64decode(value["data"][0])
            if len(data) < 115:
                counts["SHORT"] = counts.get("SHORT", 0) + 1
                continue
            qm = data[83:115]
            if qm == b"\x00" * 32:
                counts["WSOL"] = counts.get("WSOL", 0) + 1
                continue
            a = str(Pubkey(qm))
            if a == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v":
                counts["USDC"] = counts.get("USDC", 0) + 1
            else:
                counts["EXOTIC"] = counts.get("EXOTIC", 0) + 1
                unique_quotes[a] = unique_quotes.get(a, 0) + 1
                exotic.append((r["mint"][:10], r["symbol"], a[:10], len(data), r["market_cap"]))
        except Exception as e:
            counts["ERR"] = counts.get("ERR", 0) + 1
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {k:>8}: {v}")
    if unique_quotes:
        print("  unique exotic quote mints (addr: count):")
        for a, n in sorted(unique_quotes.items(), key=lambda x: -x[1]):
            print(f"    {a}: {n}")
    if exotic:
        print("  exotic samples (mint, sym, quote, len, mcap):")
        for e in exotic[:25]:
            print(f"    {e}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mint", action="append", help="token mint(s)")
    p.add_argument("--txhex", default="", help="raw tx hex to decode (optional)")
    p.add_argument("--fetch-tx", action="store_true", help="also pull recent tx for the mint/curve")
    p.add_argument("--capture-cpi", action="store_true", help="capture real pump BUY/SELL CPI accounts from recent txs")
    p.add_argument("--scan-db", metavar="DB", help="sqlite decisions db to scan")
    p.add_argument("--hours", type=int, default=24, help="scan window (default 24h)")
    p.add_argument("--limit", type=int, default=500, help="max mint rows to scan (default 500)")
    args = p.parse_args()

    settings = get_settings()
    rpc_url = settings.rpc_http
    print(f"using RPC: {'helius' if settings.helius_api_key else 'public RPC'}")

    if args.scan_db:
        scan_db(rpc_url, args.scan_db, args.hours, args.limit)
        print("(done)")
        return
    if not args.mint:
        p.error("need --mint or --scan-db")

    for m in args.mint:
        probe_curve(rpc_url, m)

    if args.txhex:
        decode_tx(rpc_url, args.txhex, args.mint[0])

    if args.fetch_tx:
        for m in args.mint:
            curve = get_bonding_curve_pda(Pubkey.from_string(m))
            fetch_recent_tx(rpc_url, m, str(curve))

    if args.capture_cpi:
        for m in args.mint:
            capture_cpi(rpc_url, m)

    print("\nquote-mint summary:")
    for m in args.mint:
        probe_curve_quote_only(rpc_url, m)
    print("(done)")


if __name__ == "__main__":
    main()