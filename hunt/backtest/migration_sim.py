from __future__ import annotations

import json
import sqlite3
import statistics
import time
from pathlib import Path

from loguru import logger

from hunt.backtest.curve import TieredConfig, entry_price_ratio, simulate_tiered, dead_trade_usd

DB_PATH = "hunt/data/hunt.sqlite3"
KLINES_DIR = Path("hunt/data/cache/klines_1m")


def load_candles(mint: str) -> list[dict]:
    p = KLINES_DIR / f"{mint}.json"
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text())
        return [c for c in d.get("candles", []) if c.get("close")]
    except Exception:
        return []


def first_post_grad_open(candles: list[dict]) -> float | None:
    for c in candles:
        if c.get("open"):
            return float(c["open"])
    return None


def run_migration_backtest() -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT mint, symbol, created_ts, first_candle_ts, candles_fetched "
        "FROM pump_grads WHERE candles_fetched>0"
    ).fetchall()
    logger.info("migration sim: {} tokens resolved", len(rows))

    base_ratio = entry_price_ratio(0.85)
    cfg = TieredConfig()
    dead_usd = dead_trade_usd(cfg)
    results: list[dict] = []

    for r in rows:
        if r["candles_fetched"] == 2:
            results.append({
                "mint": r["mint"], "symbol": r["symbol"], "exit": "dead",
                "net_usd": dead_usd, "minutes": 0, "tier_fills": 0,
            })
            continue
        candles = load_candles(r["mint"])
        grad_open = first_post_grad_open(candles)
        if not grad_open or grad_open <= 0:
            continue
        entry_usd = grad_open * base_ratio
        res = simulate_tiered(candles, entry_usd, cfg)
        res["mint"] = r["mint"]
        res["symbol"] = r["symbol"]
        results.append(res)

    summary = aggregate_usd(results)
    save_trades(results)
    logger.info("migration sim done on {} trades:\n{}", len(results), format_summary(summary))
    return {"summary": summary, "n": len(results)}


def aggregate_usd(results: list[dict]) -> dict:
    if not results:
        return {}
    pnls = [r["net_usd"] for r in results]
    wins = [p for p in pnls if p > 0]
    by_exit: dict[str, int] = {}
    for r in results:
        key = r["exit"]
        by_exit[key] = by_exit.get(key, 0) + 1
    return {
        "trades": len(pnls),
        "win_rate_pct": round(len(wins) / len(pnls) * 100, 1),
        "total_pnl_usd": round(sum(pnls), 2),
        "avg_pnl_usd": round(statistics.mean(pnls), 4),
        "median_pnl_usd": round(statistics.median(pnls), 4),
        "best_usd": round(max(pnls), 2),
        "worst_usd": round(min(pnls), 2),
        "exits": dict(sorted(by_exit.items(), key=lambda kv: -kv[1])),
        "median_minutes_to_exit": round(
            statistics.median([r.get("minutes", 0) for r in results]), 1
        ),
    }


def save_trades(results: list[dict]) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DROP TABLE IF EXISTS sim_migration_trades")
    conn.execute("""
        CREATE TABLE sim_migration_trades (
            mint TEXT, symbol TEXT, exit_type TEXT,
            net_usd REAL, minutes INTEGER, tier_fills INTEGER
        )
    """)
    conn.executemany(
        "INSERT INTO sim_migration_trades VALUES (?,?,?,?,?,?)",
        [(r["mint"], r["symbol"], r["exit"], round(r["net_usd"], 4),
          int(r.get("minutes") or 0), int(r.get("tier_fills") or 0)) for r in results],
    )
    conn.commit()
    conn.close()


def format_summary(s: dict) -> str:
    if not s:
        return "no data"
    lines = [
        f"  trades={s['trades']} win_rate={s['win_rate_pct']}%",
        f"  TOTAL PnL: ${s['total_pnl_usd']:+.2f} (on $5/trade)",
        f"  avg=${s['avg_pnl_usd']:+.4f}/trade median=${s['median_pnl_usd']:+.4f} "
        f"best=${s['best_usd']:+.2f} worst=${s['worst_usd']:+.2f}",
        f"  exits={s['exits']}",
        f"  median_exit_after={s['median_minutes_to_exit']}min",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    run_migration_backtest()
