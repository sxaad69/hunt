#!/usr/bin/env python3
"""Engine status (run from the repo root, e.g. `.venv/bin/python status.py`).

Default: decisions / open / closed+pnl from the local SQLite DB.
--reconcile: diff every open LIVE row against the actual chain bag
(associated token account balance) + list token bags with no DB row
(ORPHANs) + wallet SOL. Settles "where is the sell?" in one command.
Needs HUNT_HELIUS_API_KEY + HUNT_WALLET_PRIVATE_KEY from .env.
"""
import argparse
import base64
import json
import os
import sqlite3
import sys
import urllib.request

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
DB = os.path.join(REPO, "hunt", "data", "hunt.sqlite3")

TOKEN_KEG = "TokenkegQfeZyyvZnytVJLjH8cUIjKXqJmV8dYNR7TfSqd4EhDk"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"  # hunt/exec/pumpfun/constants.py
ASSOCIATED_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8wF"


def load_dotenv(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("'\"")
    except FileNotFoundError:
        pass
    return env


def rpc(url, method, params, timeout=25):
    q = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    req = urllib.request.Request(url, json.dumps(q).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.load(r)
    if body.get("error"):
        raise RuntimeError(body["error"])
    return body.get("result")


def b58decode(s):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = 0
    for ch in s:
        n = n * 58 + alphabet.index(ch)
    size = (n.bit_length() + 7) // 8 or 1
    raw = n.to_bytes(size, "big")
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def find_program_address(seeds, program):
    from solders.pubkey import Pubkey
    addr, bump = Pubkey.find_program_address([bytes(s) for s in seeds], Pubkey.from_string(program))
    return str(addr), bump


def wallet_pubkey_from_secret(secret):
    secret = (secret or "").strip()
    if secret.startswith("["):
        raw = bytes(json.loads(secret))
    else:
        raw = b58decode(secret)
    if len(raw) == 64:
        raw = raw[32:]
    try:
        from solders.pubkey import Pubkey
        return str(Pubkey(raw))
    except Exception:
        return None


def cmd_status():
    c = sqlite3.connect(DB)
    print("decisions:", c.execute("SELECT COUNT(*) FROM paper_decisions").fetchone()[0])
    print("open:", c.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0])
    closed = c.execute(
        "SELECT COUNT(*), ROUND(COALESCE(SUM(pnl_sol),0),4) FROM positions WHERE status='closed'").fetchone()
    print("closed:", closed[0], "| pnl:", closed[1], "SOL")


def cmd_reconcile():
    env = load_dotenv(os.path.join(REPO, ".env"))
    helius = env.get("HUNT_HELIUS_API_KEY", "")
    if not helius:
        sys.exit("HUNT_HELIUS_API_KEY missing from .env")
    url = f"https://mainnet.helius-rpc.com/?api-key={helius}"
    wallet = wallet_pubkey_from_secret(env.get("HUNT_WALLET_PRIVATE_KEY", ""))
    if not wallet:
        sys.exit("HUNT_WALLET_PRIVATE_KEY missing/unparseable in .env")
    print(f"wallet: {wallet}")
    print(f"SOL: {rpc(url, 'getBalance', [wallet])['value'] / 1e9:.6f}")
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    cols = {r[1] for r in c.execute("PRAGMA table_info(positions)").fetchall()}
    tok_col = "tokens"
    dec_sel = ("decimals" if "decimals" in cols else "6 AS decimals")
    open_live = c.execute(
        f"SELECT id, mint, symbol, {tok_col} AS tokens, {dec_sel} FROM positions"
        " WHERE mode='LIVE' AND status='open'").fetchall()
    print(f"open LIVE rows: {len(open_live)}")
    seen_mints = set()
    for r in open_live:
        mint = r["mint"]
        seen_mints.add(mint)
        info = rpc(url, "getAccountInfo", [mint, {"encoding": "base64"}])
        if not info:
            print(f"  #{r['id']} {r['symbol']}: MINT ACCOUNT GONE")
            continue
        tprog = info["owner"]
        ata, _ = find_program_address(
            [b58decode(wallet), b58decode(tprog), b58decode(mint)], ASSOCIATED_PROGRAM)
        acc = rpc(url, "getAccountInfo", [ata, {"encoding": "jsonParsed"}])
        chain_raw = 0
        if acc:
            try:
                chain_raw = int(acc["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            except Exception:
                chain_raw = -1
        dec = int(r["decimals"] or 6)
        db_raw = round(float(r["tokens"] or 0) * (10 ** dec))
        flag = "OK " if chain_raw == db_raw else "DRIFT"
        print(f"  #{r['id']} {r['symbol']}: {flag} db_raw={db_raw} chain_raw={chain_raw} ata={ata[:10]}..")
    # orphan scan: token bags with no open LIVE row (both token programs)
    orphans = 0
    for prog in (TOKEN_KEG, TOKEN_2022):
        try:
            res = rpc(url, "getTokenAccountsByOwner",
                      [wallet, {"programId": prog}, {"encoding": "jsonParsed"}])
        except Exception:
            continue
        for a in (res or {}).get("value", []) or []:
            try:
                info = a["account"]["data"]["parsed"]["info"]
                amt = int(info["tokenAmount"]["amount"])
            except Exception:
                continue
            if amt > 0 and info.get("mint") not in seen_mints:
                orphans += 1
                print(f"  ORPHAN: {info.get('mint')} raw={amt}")
    if not open_live and not orphans:
        print("flat: no open LIVE rows, no token bags")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reconcile", action="store_true", help="diff open LIVE rows vs chain")
    args = ap.parse_args()
    if args.reconcile:
        cmd_reconcile()
    else:
        cmd_status()


if __name__ == "__main__":
    main()
