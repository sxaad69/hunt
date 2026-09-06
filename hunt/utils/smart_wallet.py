from __future__ import annotations
import sqlite3
from hunt.config import get_settings

DB="hunt/data/hunt.sqlite3"

def is_tracked_wallet_mint(mint: str) -> tuple[bool, str]:
    """Check if mint was recently bought by a tracked wallet (via wallet_token_edges)."""
    try:
        conn=sqlite3.connect(DB)
        # check if any tracked wallet has edge for this mint
        rows=conn.execute("""
            SELECT w.address FROM wallets w
            JOIN wallet_token_edges e ON e.wallet=w.address
            WHERE w.status='tracked' AND e.mint=?
            LIMIT 1
        """, (mint,)).fetchall()
        conn.close()
        if rows:
            return True, rows[0][0][:8]
        return False, ""
    except:
        return False, ""

async def check_smart_buy(mint: str) -> tuple[bool, str]:
    # first check local edges (fast)
    ok, who = is_tracked_wallet_mint(mint)
    if ok:
        return True, f"tracked_{who}"
    # fallback: try GMGN live top traders for mint (30, 1 call)
    try:
        from hunt.gmgn.client import GmgnClient
        from hunt.config import get_settings
        s=get_settings()
        if not s.gmgn_api_key:
            return False, ""
        client=GmgnClient(s.gmgn_api_key)
        traders=await client.token_top_traders(mint, limit=10)
        if not traders:
            return False, ""
        # get tracked set
        conn=sqlite3.connect(DB)
        tracked={r[0] for r in conn.execute("SELECT address FROM wallets WHERE status='tracked'").fetchall()}
        conn.close()
        for t in traders[:10]:
            addr=t.get("address") or t.get("wallet") or ""
            if addr in tracked:
                return True, f"smart_{addr[:8]}"
    except:
        pass
    return False, ""
