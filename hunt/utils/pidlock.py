from __future__ import annotations
import os, json
from pathlib import Path

LOCK_PATH = Path("state/hunt.lock")

def acquire_lock() -> bool:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            data = json.loads(LOCK_PATH.read_text())
            pid = data.get("pid")
            # check if pid alive
            if pid and os.path.exists(f"/proc/{pid}"):
                return False
            # stale lock
            LOCK_PATH.unlink()
        except:
            try: LOCK_PATH.unlink()
            except: pass
    LOCK_PATH.write_text(json.dumps({"pid": os.getpid()}))
    return True

def release_lock():
    try:
        if LOCK_PATH.exists():
            LOCK_PATH.unlink()
    except: pass
