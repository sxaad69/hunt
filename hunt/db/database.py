from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'unknown',
    status TEXT NOT NULL DEFAULT 'candidate',
    score REAL,
    metrics_json TEXT,
    first_token TEXT,
    added_at INTEGER NOT NULL,
    scored_at INTEGER,
    last_active_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_wallets_status ON wallets(status);

CREATE TABLE IF NOT EXISTS seen_tokens (
    mint TEXT PRIMARY KEY,
    symbol TEXT,
    liquidity_usd REAL,
    vol24h_usd REAL,
    change24h REAL,
    pair_created_at INTEGER,
    first_seen INTEGER NOT NULL,
    last_hot_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wallet_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL,
    signature TEXT NOT NULL,
    ts INTEGER NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    side TEXT NOT NULL,
    sol_amount REAL NOT NULL,
    token_amount REAL NOT NULL,
    UNIQUE(wallet, signature, mint, side)
);
CREATE INDEX IF NOT EXISTS idx_wt_wallet ON wallet_trades(wallet);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    side TEXT NOT NULL,
    whale_sol REAL,
    action TEXT NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_sig_mint_ts ON signals(mint, ts);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    position_id INTEGER,
    mode TEXT NOT NULL,
    side TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    amount_sol REAL,
    token_amount REAL,
    price_usd REAL,
    fee_sol REAL DEFAULT 0,
    signature TEXT,
    status TEXT NOT NULL DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_ts INTEGER NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    mode TEXT NOT NULL DEFAULT 'paper',
    size_sol REAL NOT NULL,
    tokens REAL NOT NULL DEFAULT 0,
    entry_price_usd REAL,
    tp_pct REAL,
    sl_pct REAL,
    trail_pct REAL,
    peak_price_usd REAL,
    status TEXT NOT NULL DEFAULT 'open',
    closed_ts INTEGER,
    exit_reason TEXT,
    exit_sol REAL,
    pnl_sol REAL
);
CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS universe_days (
    day TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    gain_pct REAL,
    volume_usd REAL,
    PRIMARY KEY (day, mint)
);

CREATE TABLE IF NOT EXISTS wallet_token_edges (
    wallet TEXT NOT NULL,
    mint TEXT NOT NULL,
    day TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'birdeye',
    rank_in_token INTEGER,
    PRIMARY KEY (wallet, mint, day)
);
CREATE INDEX IF NOT EXISTS idx_edges_wallet ON wallet_token_edges(wallet);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    stage_days INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    trades INTEGER,
    win_rate REAL,
    pnl_sol REAL,
    metrics_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_bt_wallet ON backtest_runs(wallet, stage_days);
"""


@dataclass
class WalletTrade:
    signature: str
    ts: int
    mint: str
    symbol: Optional[str]
    side: str
    sol_amount: float
    token_amount: float


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path, timeout=20)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=20000")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "database not connected"
        return self._db

    async def kv_get(self, key: str, default: str | None = None) -> str | None:
        cur = await self.db.execute("SELECT value FROM kv WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else default

    async def kv_set(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self.db.commit()

    async def kv_get_int(self, key: str, default: int = 0) -> int:
        raw = await self.kv_get(key)
        try:
            return int(raw) if raw is not None else default
        except ValueError:
            return default

    async def kv_bump_daily(self, key: str, amount: int = 1) -> int:
        today = time.strftime("%Y-%m-%d")
        full_key = f"{key}:{today}"
        val = await self.kv_get_int(full_key) + amount
        await self.kv_set(full_key, str(val))
        return val

    async def upsert_wallet(
        self,
        address: str,
        source: str,
        status: str = "candidate",
        first_token: str | None = None,
    ) -> None:
        await self.db.execute(
            """INSERT INTO wallets(address, source, status, first_token, added_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(address) DO UPDATE SET last_active_ts=excluded.added_at""",
            (address, source, status, first_token, int(time.time())),
        )
        await self.db.commit()

    async def get_wallet(self, address: str) -> Optional[aiosqlite.Row]:
        cur = await self.db.execute("SELECT * FROM wallets WHERE address=?", (address,))
        return await cur.fetchone()

    async def get_wallets_by_status(self, *statuses: str) -> list[aiosqlite.Row]:
        q = ",".join("?" for _ in statuses)
        cur = await self.db.execute(f"SELECT * FROM wallets WHERE status IN ({q})", statuses)
        return list(await cur.fetchall())

    async def count_wallets_by_status(self, status: str) -> int:
        cur = await self.db.execute("SELECT COUNT(*) c FROM wallets WHERE status=?", (status,))
        row = await cur.fetchone()
        return row["c"] if row else 0

    async def update_wallet_score(self, address: str, score: float, metrics: dict[str, Any]) -> None:
        await self.db.execute(
            "UPDATE wallets SET score=?, metrics_json=?, scored_at=? WHERE address=?",
            (score, json.dumps(metrics), int(time.time()), address),
        )
        await self.db.commit()

    async def set_wallet_status(self, address: str, status: str) -> None:
        await self.db.execute("UPDATE wallets SET status=? WHERE address=?", (status, address))
        await self.db.commit()

    async def top_candidates(self, limit: int) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            "SELECT * FROM wallets WHERE status='candidate' AND score IS NOT NULL "
            "ORDER BY score DESC LIMIT ?",
            (limit,),
        )
        return list(await cur.fetchall())

    async def mark_seen_token(
        self,
        mint: str,
        symbol: str | None,
        liquidity_usd: float,
        vol24h_usd: float,
        change24h: float,
        pair_created_at: int | None,
    ) -> bool:
        now = int(time.time())
        cur = await self.db.execute("SELECT 1 FROM seen_tokens WHERE mint=?", (mint,))
        exists = await cur.fetchone()
        await self.db.execute(
            """INSERT INTO seen_tokens(mint,symbol,liquidity_usd,vol24h_usd,change24h,pair_created_at,first_seen,last_hot_ts)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(mint) DO UPDATE SET last_hot_ts=excluded.last_hot_ts,
                 liquidity_usd=excluded.liquidity_usd, vol24h_usd=excluded.vol24h_usd""",
            (mint, symbol, liquidity_usd, vol24h_usd, change24h, pair_created_at, now, now),
        )
        await self.db.commit()
        return not exists

    async def save_wallet_trades(self, wallet: str, trades: list[WalletTrade]) -> int:
        inserted = 0
        for t in trades:
            cur = await self.db.execute(
                """INSERT OR IGNORE INTO wallet_trades(wallet,signature,ts,mint,symbol,side,sol_amount,token_amount)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (wallet, t.signature, t.ts, t.mint, t.symbol, t.side, t.sol_amount, t.token_amount),
            )
            inserted += cur.rowcount if cur.rowcount > 0 else 0
        await self.db.commit()
        return inserted

    async def get_wallet_trades(self, wallet: str, since_ts: int = 0) -> list[WalletTrade]:
        cur = await self.db.execute(
            "SELECT * FROM wallet_trades WHERE wallet=? AND ts>=? ORDER BY ts ASC",
            (wallet, since_ts),
        )
        rows = await cur.fetchall()
        return [
            WalletTrade(
                signature=r["signature"],
                ts=r["ts"],
                mint=r["mint"],
                symbol=r["symbol"],
                side=r["side"],
                sol_amount=r["sol_amount"],
                token_amount=r["token_amount"],
            )
            for r in rows
        ]

    async def wallet_trades_count(self, wallet: str) -> int:
        cur = await self.db.execute("SELECT COUNT(*) c FROM wallet_trades WHERE wallet=?", (wallet,))
        row = await cur.fetchone()
        return row["c"] if row else 0

    async def insert_signal(
        self,
        wallet: str,
        mint: str,
        symbol: str | None,
        side: str,
        whale_sol: float | None,
        action: str,
        reason: str | None = None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO signals(ts,wallet,mint,symbol,side,whale_sol,action,reason) VALUES(?,?,?,?,?,?,?,?)",
            (int(time.time()), wallet, mint, symbol, side, whale_sol, action, reason),
        )
        await self.db.commit()

    async def last_signal_age_s(self, wallet: str, mint: str) -> float | None:
        cur = await self.db.execute(
            "SELECT MAX(ts) mts FROM signals WHERE wallet=? AND mint=?",
            (wallet, mint),
        )
        row = await cur.fetchone()
        if row and row["mts"]:
            return max(0.0, time.time() - row["mts"])
        return None

    async def open_position_for_mint(self, mint: str, mode: str) -> Optional[aiosqlite.Row]:
        cur = await self.db.execute(
            "SELECT * FROM positions WHERE mint=? AND mode=? AND status='open' LIMIT 1",
            (mint, mode),
        )
        return await cur.fetchone()

    async def count_open_positions(self, mode: str) -> int:
        cur = await self.db.execute(
            "SELECT COUNT(*) c FROM positions WHERE status='open' AND mode=?", (mode,)
        )
        row = await cur.fetchone()
        return row["c"] if row else 0

    async def create_position(
        self,
        mint: str,
        symbol: str | None,
        mode: str,
        size_sol: float,
        tokens: float,
        entry_price_usd: float | None,
        tp_pct: float,
        sl_pct: float,
        trail_pct: float,
    ) -> int:
        cur = await self.db.execute(
            """INSERT INTO positions(opened_ts,mint,symbol,mode,size_sol,tokens,entry_price_usd,
               tp_pct,sl_pct,trail_pct,peak_price_usd)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(time.time()), mint, symbol, mode, size_sol, tokens,
                entry_price_usd, tp_pct, sl_pct, trail_pct, entry_price_usd,
            ),
        )
        await self.db.commit()
        return cur.lastrowid or 0

    async def get_open_positions(self, mode: str) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            "SELECT * FROM positions WHERE status='open' AND mode=?", (mode,)
        )
        return list(await cur.fetchall())

    async def update_position_after_buy(
        self, position_id: int, tokens_added: float, sol_spent: float, price_usd: float | None
    ) -> None:
        await self.db.execute(
            "UPDATE positions SET tokens=tokens+?, size_sol=size_sol+?, entry_price_usd=?, peak_price_usd=? WHERE id=?",
            (tokens_added, sol_spent, price_usd, price_usd, position_id),
        )
        await self.db.commit()

    async def update_position_peak(self, position_id: int, price_usd: float) -> None:
        await self.db.execute(
            "UPDATE positions SET peak_price_usd=MAX(COALESCE(peak_price_usd,0),?) WHERE id=?",
            (price_usd, position_id),
        )
        await self.db.commit()

    async def close_position(
        self, position_id: int, exit_sol: float, exit_reason: str
    ) -> None:
        cur = await self.db.execute("SELECT size_sol FROM positions WHERE id=?", (position_id,))
        row = await cur.fetchone()
        pnl = (exit_sol - row["size_sol"]) if row else 0.0
        await self.db.execute(
            "UPDATE positions SET status='closed', closed_ts=?, exit_reason=?, exit_sol=?, pnl_sol=? WHERE id=?",
            (int(time.time()), exit_reason, exit_sol, pnl, position_id),
        )
        await self.db.commit()

    async def insert_trade(
        self,
        mode: str,
        side: str,
        mint: str,
        symbol: str | None,
        amount_sol: float | None,
        token_amount: float | None,
        price_usd: float | None,
        position_id: int | None,
        signature: str | None = None,
        status: str = "ok",
        fee_sol: float = 0.0,
    ) -> int:
        cur = await self.db.execute(
            """INSERT INTO trades(ts,position_id,mode,side,mint,symbol,amount_sol,token_amount,
               price_usd,fee_sol,signature,status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(time.time()), position_id, mode, side, mint, symbol, amount_sol,
                token_amount, price_usd, fee_sol, signature, status,
            ),
        )
        await self.db.commit()
        return cur.lastrowid or 0

    async def daily_realized_pnl_sol(self, mode: str) -> float:
        day_start = int(time.time()) - (int(time.time()) % 86400)
        cur = await self.db.execute(
            "SELECT COALESCE(SUM(pnl_sol),0) p FROM positions WHERE mode=? AND status='closed' AND closed_ts>=?",
            (mode, day_start),
        )
        row = await cur.fetchone()
        return row["p"] if row else 0.0

    async def total_stats(self, mode: str) -> dict[str, Any]:
        cur = await self.db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(pnl_sol),0) pnl FROM positions WHERE mode=? AND status='closed'",
            (mode,),
        )
        row = await cur.fetchone()
        open_cur = await self.db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(size_sol),0) sol FROM positions WHERE mode=? AND status='open'",
            (mode,),
        )
        o = await open_cur.fetchone()
        return {
            "closed_trades": row["n"],
            "realized_pnl_sol": round(row["p"], 4),
            "open_positions": o["n"],
            "open_sol": round(o["sol"], 4),
        }

    async def tracked_wallet_addresses(self) -> list[str]:
        rows = await self.get_wallets_by_status("tracked")
        return [r["address"] for r in rows]

    async def save_universe_day(
        self, day: str, mint: str, symbol: str | None, gain_pct: float, volume_usd: float
    ) -> None:
        await self.db.execute(
            """INSERT INTO universe_days(day,mint,symbol,gain_pct,volume_usd)
               VALUES(?,?,?,?,?)
               ON CONFLICT(day,mint) DO UPDATE SET gain_pct=excluded.gain_pct,
                 volume_usd=excluded.volume_usd, symbol=excluded.symbol""",
            (day, mint, symbol, gain_pct, volume_usd),
        )

    async def commit_universe(self) -> None:
        await self.db.commit()

    async def universe_tokens(self) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            "SELECT mint, MAX(symbol) symbol, COUNT(*) days_won FROM universe_days "
            "GROUP BY mint ORDER BY days_won DESC"
        )
        return list(await cur.fetchall())

    async def universe_token_days(self, mint: str) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            "SELECT day, gain_pct, volume_usd FROM universe_days WHERE mint=? ORDER BY day DESC",
            (mint,),
        )
        return list(await cur.fetchall())

    async def save_edge(
        self, wallet: str, mint: str, day: str, source: str, rank_in_token: int
    ) -> None:
        await self.db.execute(
            """INSERT OR IGNORE INTO wallet_token_edges(wallet,mint,day,source,rank_in_token)
               VALUES(?,?,?,?,?)""",
            (wallet, mint, day, source, rank_in_token),
        )

    async def commit_edges(self) -> None:
        await self.db.commit()

    async def wallet_edge_stats(self) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            """SELECT wallet, COUNT(DISTINCT mint) tokens, COUNT(DISTINCT day) days,
                      COUNT(*) edges
               FROM wallet_token_edges GROUP BY wallet ORDER BY days DESC, edges DESC"""
        )
        return list(await cur.fetchall())

    async def save_backtest_run(
        self,
        stage_days: int,
        wallet: str,
        trades: int,
        win_rate: float,
        pnl_sol: float,
        metrics_json: str,
    ) -> None:
        await self.db.execute(
            """INSERT INTO backtest_runs(ts,stage_days,wallet,trades,win_rate,pnl_sol,metrics_json)
               VALUES(?,?,?,?,?,?,?)""",
            (int(time.time()), stage_days, wallet, trades, win_rate, pnl_sol, metrics_json),
        )
        await self.db.commit()

    async def latest_backtest(self, wallet: str) -> Optional[aiosqlite.Row]:
        cur = await self.db.execute(
            "SELECT * FROM backtest_runs WHERE wallet=? ORDER BY ts DESC LIMIT 1",
            (wallet,),
        )
        return await cur.fetchone()

    def new_id(self) -> str:
        return uuid.uuid4().hex[:12]
