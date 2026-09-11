"""Daily Telegram digest for the hunt campaign (server-side).

Run by systemd timer (08:00 UTC) or manually: python -m hunt.notify.digest
Reads the last 24h from the DB and sends the scoreboard + intel learning
notes to Telegram. No gate changes are made here — tuning happens in the
reviewed ZCode digest, this is the always-on report.
"""
from __future__ import annotations

import html
import json
import sqlite3
import time

import httpx
from loguru import logger

from hunt.config import get_settings

DB = "hunt/data/hunt.sqlite3"
SOL_FALLBACK = 104.0


def _rows(conn: sqlite3.Connection, q: str, args=()):
    return conn.execute(q, args).fetchall()


def build_digest() -> str:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cutoff = time.time() - 24 * 3600
    L: list[str] = []
    L.append("hunt daily digest (24h)")

    tot = conn.execute("SELECT COUNT(*) c, SUM(CASE WHEN decision='ACCEPT' THEN 1 ELSE 0 END) a FROM paper_decisions WHERE decided_at>=?", (cutoff,)).fetchone()
    L.append(f"scanned {tot['c']} | accepted {tot['a'] or 0} ({(tot['a'] or 0)/max(tot['c'],1)*100:.1f}%)")

    reasons = _rows(conn, "SELECT CASE WHEN reason LIKE 'dust%' THEN 'dust' ELSE substr(reason,1,14) END r, COUNT(*) c FROM paper_decisions WHERE decided_at>=? AND decision='REJECT' GROUP BY 1 ORDER BY c DESC LIMIT 5", (cutoff,))
    L.append("rejects: " + ", ".join(f"{x['r']}×{x['c']}" for x in reasons))

    o = conn.execute("SELECT COUNT(*) c FROM positions WHERE status='open'").fetchone()["c"]
    cl = conn.execute("SELECT COUNT(*) c, ROUND(SUM(pnl_sol),4) p FROM positions WHERE status='closed' AND opened_ts>=?", (cutoff,)).fetchone()
    L.append(f"positions: open {o} | closed {cl['c']} | pnl {cl['p'] or 0:+.4f} SOL")
    for r in _rows(conn, "SELECT exit_reason, COUNT(*) c, ROUND(SUM(pnl_sol),4) tot FROM positions WHERE opened_ts>=? AND status='closed' GROUP BY 1 ORDER BY tot", (cutoff,)):
        L.append(f"  {r['exit_reason']}: ×{r['c']} {r['tot']:+.4f} SOL")

    top = conn.execute("SELECT symbol, ROUND(pnl_sol,4) pnl, ROUND(COALESCE(entry_price_sol,0)*1e9,0) mc FROM positions WHERE opened_ts>=? AND status='closed' ORDER BY pnl_sol DESC LIMIT 1", (cutoff,)).fetchone()
    if top:
        L.append(f"top runner: {top['symbol']} {top['pnl']:+.4f} SOL (entry {top['mc']:.0f} SOL)")

    # intel fingerprints: winners vs losers at decision time
    w = conn.execute("""SELECT AVG(pd.top10) t, AVG(pd.holders) h,
                               AVG(json_extract(pd.tracker_json,'$.snipers.totalPercentage')) s, COUNT(*) c
                        FROM paper_decisions pd JOIN positions po ON po.mint=pd.mint
                        WHERE pd.decided_at>=? AND po.pnl_sol>0 AND pd.top10 IS NOT NULL""", (cutoff,)).fetchone()
    l = conn.execute("""SELECT AVG(pd.top10) t, AVG(pd.holders) h,
                               AVG(json_extract(pd.tracker_json,'$.snipers.totalPercentage')) s, COUNT(*) c
                        FROM paper_decisions pd JOIN positions po ON po.mint=pd.mint
                        WHERE pd.decided_at>=? AND po.pnl_sol<=0 AND pd.top10 IS NOT NULL""", (cutoff,)).fetchone()
    if w["c"] and l["c"]:
        ws = f"{w['s']:.1f}" if w["s"] is not None else "?"
        ls = f"{l['s']:.1f}" if l["s"] is not None else "?"
        L.append(f"intel: winners(n={w['c']}) top10={w['t']:.0f}% holders={w['h']:.0f} snip%={ws} | losers(n={l['c']}) top10={l['t']:.0f}% holders={l['h']:.0f} snip%={ls}")
    else:
        b = conn.execute("SELECT COUNT(*) c FROM paper_decisions WHERE top10 IS NOT NULL AND decided_at>=?", (cutoff,)).fetchone()["c"]
        L.append(f"intel: {b} fingerprints collected (win/loss split needs more samples)")

    for flag in ("serial_rugger", "dev_winner"):
        n = conn.execute("SELECT COUNT(*) c FROM paper_decisions WHERE reason LIKE ? AND decided_at>=?", (f"%{flag}%", cutoff)).fetchone()["c"]
        if n:
            L.append(f"{flag}: {n}")

    sr = conn.execute("SELECT COUNT(*) c FROM paper_decisions WHERE reason LIKE 'mcap_ceiling%' AND decided_at>=?", (cutoff,)).fetchone()["c"]
    L.append(f"species-B observed (ceiling rejects): {sr}")

    conn.close()
    return "\n".join(L)


def send(text: str):
    s = get_settings()
    if not (s.telegram_bot_token and s.telegram_chat_id):
        logger.warning("telegram not configured — digest printed only")
        print(text)
        return
    r = httpx.post(
        f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
        json={"chat_id": s.telegram_chat_id, "text": html.escape(text)},
        timeout=15)
    logger.info("digest sent: {}", r.status_code)


if __name__ == "__main__":
    try:
        text = build_digest()
    except Exception as e:
        text = f"hunt digest ERROR: {e}"
        logger.exception("digest build failed")
    send(text)
