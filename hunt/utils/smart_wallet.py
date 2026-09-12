from __future__ import annotations
import sqlite3

DB = "hunt/data/hunt.sqlite3"


def is_tracked_wallet_mint(mint: str) -> tuple[bool, str]:
    try:
        conn = sqlite3.connect(DB)
        rows = conn.execute(
            """
            SELECT w.address FROM wallets w
            JOIN wallet_token_edges e ON e.wallet=w.address
            WHERE w.status='tracked' AND w.source='gmgn_smartmoney'
              AND e.source='gmgn_smartmoney' AND e.mint=?
            LIMIT 1
            """,
            (mint,),
        ).fetchall()
        conn.close()
        if rows:
            return True, rows[0][0][:8]
        return False, ""
    except Exception:
        return False, ""


async def check_smart_buy(mint: str) -> tuple[bool, str]:
    ok, who = is_tracked_wallet_mint(mint)
    if ok:
        return True, f"tracked_{who}"
    return False, ""
