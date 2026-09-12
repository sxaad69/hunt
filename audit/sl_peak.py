#!/usr/bin/env python3
"""Today's SL closes vs peak mcap (no OHLCV replay).

For the last N stop-loss fills, join our entry snapshot to GMGN ATH + current
mcap. Answers: after we SL'd, did the coin ever print a peak above entry?

  fold_now = current SOL mcap / entry SOL mcap
  fold_ath = GMGN ath_price * supply (in SOL) / entry SOL mcap

Usage: .venv/bin/python audit/sl_peak.py [n=7] [hours=24]
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

N = int(sys.argv[1]) if len(sys.argv) > 1 else 7
HOURS = int(sys.argv[2]) if len(sys.argv) > 2 else 24
DB = "hunt/data/hunt.sqlite3"


def fetch(url, **kw):
    with httpx.Client(timeout=15, headers={"accept": "application/json",
                                           "user-agent": "Mozilla/5.0"}) as cl:
        return cl.get(url, **kw).json()


def sol_price() -> float:
    for url in ("https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT",
                "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"):
        try:
            j = fetch(url)
            p = float(j.get("price") or (j.get("solana") or {}).get("usd") or 0)
            if p > 0:
                return p
        except Exception:
            continue
    return 0.0


def _f(x) -> float | None:
    if x is None or x == "":
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def gmgn_peaks(info: dict | None, sol: float) -> tuple[float | None, float | None]:
    """Return (now_sol, ath_sol) from token info."""
    if not info or not isinstance(info, dict):
        return None, None
    if "address" not in info and isinstance(info.get("data"), dict):
        info = info["data"]
    supply = _f(info.get("total_supply") or info.get("circulating_supply"))
    px = info.get("price")
    now_usd = None
    if isinstance(px, dict):
        now_usd = _f(px.get("price"))
    elif px is not None:
        now_usd = _f(px)
    ath_usd = _f(info.get("ath_price"))
    now_sol = ath_sol = None
    if supply and sol > 0:
        if now_usd:
            now_sol = (now_usd * supply) / sol
        if ath_usd:
            ath_sol = (ath_usd * supply) / sol
    return now_sol, ath_sol


def pump_now_sol(mint: str, sol: float) -> float | None:
    try:
        j = fetch(f"https://frontend-api-v3.pump.fun/coins/{mint}")
    except Exception:
        return None
    usd = _f((j or {}).get("usd_market_cap"))
    if usd and sol > 0:
        return usd / sol
    mc = _f((j or {}).get("market_cap"))
    return mc


def verdict(fold_ath: float | None, fold_now: float | None, hold_x: float) -> str:
    ath = fold_ath or 0.0
    now = fold_now or 0.0
    if ath >= 2.0 and ath > hold_x * 1.3:
        return "PUMPED after SL"
    if ath >= 1.4:
        return "ran above entry"
    if now < 0.2:
        return "dusted"
    if now < 0.8:
        return "faded"
    if now <= 1.2:
        return "flat"
    return "still up"


async def load_gmgn(mints: list[str]) -> dict[str, dict]:
    from hunt.gmgn.client import GmgnClient
    client = GmgnClient()
    out: dict[str, dict] = {}
    for m in mints:
        try:
            info = await client.token_info(m)
        except Exception:
            info = None
        if info:
            out[m] = info
    return out


def main():
    sol = sol_price()
    since = int(time.time()) - HOURS * 3600
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    total_sl = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE exit_reason='stop_loss' AND closed_ts>=?",
        (since,),
    ).fetchone()[0]
    rows = conn.execute(
        """SELECT p.symbol, p.mint, p.opened_ts, p.closed_ts, p.size_sol, p.pnl_sol,
                  p.entry_price_sol, p.peak_price_sol,
                  d.market_cap AS entry_mcap, d.reason
           FROM positions p
           LEFT JOIN paper_decisions d ON d.mint=p.mint
           WHERE p.exit_reason='stop_loss' AND p.closed_ts>=?
           ORDER BY p.closed_ts DESC
           LIMIT ?""",
        (since, N),
    ).fetchall()
    mints = [r["mint"] for r in rows]
    gmgn = asyncio.run(load_gmgn(mints)) if mints else {}

    print(f"SL peak-mcap audit: last {len(rows)} of {total_sl} stop-losses in {HOURS}h  |  SOL=${sol:,.2f}")
    print("fold_ath = GMGN ATH mcap / our entry SOL mcap   |   hold_x = peak while we were in")
    print()
    print(f"{'#':>2s} {'token':12s} {'held':>5s} {'entry':>8s} {'hold_x':>6s} {'now':>8s} {'ath':>8s} "
          f"{'now/e':>6s} {'ath/e':>6s}  verdict")

    pumped = 0
    for i, r in enumerate(rows, 1):
        entry = float(r["entry_mcap"] or 0)
        ep = float(r["entry_price_sol"] or 0)
        pk = float(r["peak_price_sol"] or 0)
        hold_x = (pk / ep) if ep > 0 and pk > 0 else 1.0
        held_s = max(0, int(r["closed_ts"] or 0) - int(r["opened_ts"] or 0))
        held = f"{held_s}s" if held_s < 120 else f"{held_s/60:.0f}m"
        now_sol, ath_sol = gmgn_peaks(gmgn.get(r["mint"]), sol)
        if now_sol is None:
            now_sol = pump_now_sol(r["mint"], sol)
        fold_now = (now_sol / entry) if now_sol and entry > 0 else None
        fold_ath = (ath_sol / entry) if ath_sol and entry > 0 else None
        tag = verdict(fold_ath, fold_now, hold_x)
        if tag.startswith("PUMP"):
            pumped += 1
        def fmt(v, n=8):
            if v is None:
                return "-".rjust(n)
            return f"{v:,.0f}".rjust(n) if v >= 10 else f"{v:,.1f}".rjust(n)
        fn = f"{fold_now:5.2f}" if fold_now is not None else "    -"
        fa = f"{fold_ath:5.2f}" if fold_ath is not None else "    -"
        print(f"{i:>2d} {(r['symbol'] or '?'):12s} {held:>5s} {fmt(entry)} {hold_x:5.2f}x "
              f"{fmt(now_sol)} {fmt(ath_sol)} {fn:>6s} {fa:>6s}  {tag}")

    print(f"\n{pumped}/{len(rows)} printed an ATH ≥2x entry after we were already stopped")
    conn.close()


if __name__ == "__main__":
    main()
