"""Tier PnL must include LIVE realized losses (Apple -0.056 never throttled
the bot because only PAPER counted). Scratch DBs — no network, no money.
"""
import os
import sqlite3
import tempfile

import hunt.survival.tiers as tiers


def _scratch(rows):
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE positions (mode TEXT, status TEXT, pnl_sol REAL)")
    c.executemany("INSERT INTO positions VALUES (?,?,?)", rows)
    c.commit()
    c.close()
    return path


def test_live_realized_counts():
    path = _scratch([
        ("PAPER", "closed", 2.0), ("PAPER", "closed", -0.5), ("PAPER", "open", None),
        ("LIVE", "closed", -0.056156934), ("LIVE", "open", None),
    ])
    old = tiers.DB_PATH
    tiers.DB_PATH = path
    try:
        assert abs(tiers.current_pnl_sol() - 1.443843066) < 1e-9
    finally:
        tiers.DB_PATH = old
        os.remove(path)


def test_tier_thresholds():
    assert tiers.tier_for_pnl(0.0).name == "normal"
    assert tiers.tier_for_pnl(-0.3).name == "low_compute"
    assert tiers.tier_for_pnl(-0.6).name == "critical"
    assert tiers.tier_for_pnl(-2.0).name == "dead"
    # live-sized loss alone must move the tier off normal
    assert tiers.tier_for_pnl(-0.25).name != "normal"
