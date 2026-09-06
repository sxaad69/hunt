from __future__ import annotations
import json, time, os
from pathlib import Path

STATE_PATH = Path("state/creators.json")

def load() -> dict:
    if not STATE_PATH.exists(): return {}
    try: return json.loads(STATE_PATH.read_text())
    except: return {}

def save(data: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, STATE_PATH)

def record_close(deployer: str, pnl_sol: float, rug_loss_pct: float = -25.0, block_after: int = 2):
    if not deployer: return
    data = load()
    entry = data.get(deployer, {"rugs": 0, "total": 0, "last_ts": 0})
    entry["total"] += 1
    entry["last_ts"] = int(time.time())
    if pnl_sol <= rug_loss_pct/100 * 0.05:  # approx 5% of position, heuristic
        entry["rugs"] = entry.get("rugs", 0) + 1
    data[deployer] = entry
    save(data)

def is_blocked(deployer: str, block_after: int = 2, forget_days: int = 30) -> tuple[bool, str]:
    if not deployer: return False, "no_deployer"
    data = load()
    entry = data.get(deployer)
    if not entry: return False, "clean"
    if entry.get("rugs", 0) >= block_after:
        return True, f"rug_creator_{entry['rugs']}rugs"
    # forget clean after days
    if entry.get("rugs", 0) == 0 and time.time() - entry.get("last_ts", 0) > forget_days*86400:
        return False, "forgotten"
    return False, "pass"
