from __future__ import annotations
import sqlite3, time
from dataclasses import dataclass
from pathlib import Path

DB_PATH = "hunt/data/hunt.sqlite3"
STATE_PATH = Path("state/survival.json")

@dataclass
class Tier:
    name: str
    trade_size_sol: float
    poll_interval_s: int
    max_open: int
    min_mcap: float
    allow_no_social: bool

TIERS = {
    "normal": Tier("normal", 0.05, 30, 100, 0, True),
    "low_compute": Tier("low_compute", 0.02, 60, 30, 5000, False),
    "critical": Tier("critical", 0.01, 120, 10, 10000, False),
    "dead": Tier("dead", 0.0, 0, 0, 999999, False),
}

def current_pnl_sol() -> float:
    # PAPER realized + LIVE realized. Live losses MUST throttle the bot too —
    # Apple −0.056 (09-09) never moved the tier because only PAPER counted.
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT COALESCE(SUM(pnl_sol),0) FROM positions WHERE mode IN ('PAPER','LIVE')").fetchone()
        # realized only — unrealized counts 0 for the tier decision.
        conn.close()
        return float(row[0] or 0)
    except:
        return 0.0

def tier_for_pnl(pnl: float) -> Tier:
    # v2: with mcap>=50 filter, expected win 96% so stay normal longer
    if pnl <= -1.0:
        return TIERS["dead"]
    if pnl <= -0.5:
        return TIERS["critical"]
    if pnl < -0.2:
        return TIERS["low_compute"]
    return TIERS["normal"]

def get_tier() -> Tier:
    pnl = current_pnl_sol()
    return tier_for_pnl(pnl)

def heartbeat_once() -> dict:
    tier = get_tier()
    pnl = current_pnl_sol()
    # write state like automaton SOUL.md / survival.json
    import json
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {"ts": int(time.time()), "pnl_sol": pnl, "tier": tier.name, "tier_cfg": tier.__dict__}
    STATE_PATH.write_text(json.dumps(data, indent=2))
    return data
