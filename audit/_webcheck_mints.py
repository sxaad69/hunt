import asyncio
import httpx
from hunt.config import get_settings
from hunt.paper.onchain_intel import fetch_onchain_top10

MINTS = [
    ("tst", "9jnYhH9rqSa9RPJ2S2KPJCnvaDVaVbMhmzSiWooSpump"),
    ("stocklana", "HhNsmp6SoeMZ3S3SJCPpJcTbDJf7KgSPGk9V12Uwpump"),
    ("ily", "tp1KcxzCAdq5Cu8EqjrR7nLwSfisETEC8q4JiXXpump"),
    ("HERD", "8EmWFiBdU2xbVPtmuFtYLCfV8AccyPMcFKoygyD4pump"),
]

async def main():
    s = get_settings()
    async with httpx.AsyncClient(timeout=20, headers={"user-agent": "hunt-lab/1"}) as c:
        for n, m in MINTS:
            idx = await c.get(f"https://advanced-indexer.pump.fun/in-memory-coin/{m}")
            d = idx.json() if idx.status_code == 200 else {}
            chain = await fetch_onchain_top10(c, s.rpc_http, m)
            print(n, "idx_top10=", d.get("top10HoldersPercent"),
                  "idx_holders=", d.get("numHolders"),
                  "idx_snip=", d.get("sniperCount"),
                  "chain_circ_top10=", chain)

asyncio.run(main())
