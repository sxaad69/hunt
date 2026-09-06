#!/usr/bin/env python3
"""Replicate: fork 2 paper variants (grad-only vs live-curve) with lineage, automaton style."""
import asyncio, subprocess, time, json, sqlite3
from pathlib import Path

DB="hunt/data/hunt.sqlite3"
LINEAGE=Path("state/lineage.json")

async def run_variant(name: str, hours: int = 1):
    env = {"HUNT_VARIANT": name}
    # we encode variant via file flag; paper/run.py reads HUNT_VARIANT env
    proc = await asyncio.create_subprocess_exec(
        "caffeinate", "-i", ".venv/bin/python", "-u", "-m", "hunt.paper.run", str(hours*3600),
        env={**__import__("os").environ, **env}
    )
    await proc.wait()
    return proc.returncode

def record_lineage(parent: str, child: str, pnl: float):
    LINEAGE.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(LINEAGE.read_text()) if LINEAGE.exists() else []
    data.append({"ts": int(time.time()), "parent": parent, "child": child, "pnl": pnl})
    LINEAGE.write_text(json.dumps(data, indent=2))

async def main():
    # launch both variants in parallel (separate DB namespaces via mode suffix)
    # for now run sequentially to avoid pump 429
    print("forking grad-only variant")
    await run_variant("grad", 1)
    print("forking live-curve variant")
    await run_variant("live", 1)
    # compare
    conn=sqlite3.connect(DB)
    for mode in ("PAPER",):
        rows=conn.execute("SELECT COUNT(*), COALESCE(SUM(pnl_sol),0) FROM positions WHERE mode=? AND status='closed'", (mode,)).fetchall()
        print(rows)

if __name__=="__main__":
    asyncio.run(main())
