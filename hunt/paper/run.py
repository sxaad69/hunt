from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

import httpx
from loguru import logger

DB_PATH = "hunt/data/hunt.sqlite3"
API = "https://frontend-api-v3.pump.fun/coins"
REJECT_FILE = Path("a.txt")
REJECT_FILE2 = Path("hunt/data/paper_rejected.txt")
PAPER_DB_TABLE = "paper_decisions"

_LIVE_DECIMALS: dict[str, int] = {}
_LIVE_HALTED = False  # set after hitting the daily loss cap — blocks new live opens


def _current_mode() -> str:
    """'LIVE' when HUNT_DRY_RUN=false, 'PAPER' otherwise."""
    from hunt.exec.live import live_enabled
    return "LIVE" if live_enabled() else "PAPER"


async def _live_decimals(ex, mint: str) -> int:
    if mint not in _LIVE_DECIMALS:
        _LIVE_DECIMALS[mint] = await ex._token_decimals(mint)
    return _LIVE_DECIMALS[mint]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {PAPER_DB_TABLE} (
    mint TEXT PRIMARY KEY,
    symbol TEXT,
    created_ts INTEGER,
    decision TEXT,
    reason TEXT,
    twitter TEXT,
    telegram TEXT,
    website TEXT,
    market_cap REAL,
    decided_at INTEGER,
    dev TEXT,
    burst TEXT,
    top10 REAL,
    snipers INTEGER,
    holders INTEGER,
    dev_pct REAL
);
"""


def survival_filter(coin: dict) -> tuple[bool, str]:
    """Return (accept, reason). Code gates: front filter + curve + social + model."""
    # 1. front cheap gates (age, metadata, buyers proxy)
    desc = coin.get("description") or coin.get("text") or ""
    image = coin.get("image_uri") or coin.get("imageUri") or coin.get("image") or ""
    # if coin has no description and no image, treat as metadata empty
    if not desc and not image:
        # still allow if socials exist, but mark low quality - don't hard veto here to keep permissive
        pass
    # age gate: if created within 90s, too fresh (sniper) - delay, mark as skip for now but still process
    # we don't hard reject on age, just record
    # 2. curve gate: if market_cap tiny vs 69k grad, treat as thin
    try:
        mc = float(coin.get("market_cap") or 0)
        if mc and mc < 5000:
            # very thin curve - still allow but will be caught by survival model weight
            pass
    except: pass
    # 2b. HARD VETO: dust mcap <50 is 90% losers (avg -$0.556, win 41% vs >=50 win 96.5%)
    try:
        mc = float(coin.get("market_cap") or 0)
        if mc is not None and mc < 50:
            return False, f"dust_mcap_{mc:.0f}"
        if mc >= 50:
            # high enough to override no_socials — mcap>=50 alone is 96.5% win, no need for socials
            # let it pass to model, but ensure it doesn't get rejected for no_socials
            pass
    except: pass
    # 2d. MCAP CEILING: DISABLED 2026-09-08 — reopening species-B to chase
    # USUR-class moonshots again (see AGENTS.md). Previously a hard veto:
    #   try:
    #       mc = float(coin.get("market_cap") or 0)
    #       if mc > MCAP_CEILING_SOL:
    #           return False, f"mcap_ceiling_{mc:.0f}"
    #   except: pass
    # KNOWN RISK: species-B round-dumps slip past -20% SL -> full-stake losses
    # (6 full-stake SLs on 09-05). Measure SL-death rate (survival-agnostic)
    # before trusting this; one evidence-backed step, log in daily digest.
    # 2e. distribution/bundle gates from in-memory-coin intel (fields provided
    # by the decision-time enrichment; absent for poll-path coins -> no veto)
    try:
        if float(coin.get("_top10") or 0) > 75:
            return False, "top10_heavy"
        if int(coin.get("_snipers") or 0) >= 2:
            return False, "sniper_bundle"
    except: pass
    # 2c. DEMAND GATE: require a recent trade (kill dead-on-arrival live-curve tokens
    # that never pump — 13/42 SL losers had peak<=0% and no demand). 180s staleness veto.
    try:
        ltt = coin.get("last_trade_timestamp")
        if ltt:
            stale_s = (time.time()*1000 - float(ltt)) / 1000.0
            if stale_s > 180.0:
                return False, f"stale_no_trade_{stale_s:.0f}s"
    except: pass
    # 3. survival model (social + dead_hour + high_mcap weighted)
    try:
        from hunt.paper.survival_model import predict
        twitter = coin.get("twitter") or ""
        telegram = coin.get("telegram") or ""
        website = coin.get("website") or ""
        created_ts = int(coin.get("created_timestamp") or 0)//1000
        mc = float(coin.get("market_cap") or 0) or None
        p, _ = predict(twitter, telegram, website, created_ts, mc)
        if p < 0.5:
            # map to original reasons for a.txt compatibility
                    has_social = bool(twitter.strip() or telegram.strip() or website.strip())
                    if not has_social:
                        # high_mcap already handled above, so this is low mcap no_socials → truly weak
                        return False, "no_socials"
                    ts_ms = coin.get("created_timestamp")
                    if ts_ms:
                        hour = time.gmtime(int(ts_ms) // 1000).tm_hour
                        if hour in (3, 5):
                            return False, f"dead_hour_{hour:02d}UTC"
                    return False, f"low_p_{p:.2f}"
    except Exception:
        pass
    # fallback legacy
    twitter = coin.get("twitter") or ""
    telegram = coin.get("telegram") or ""
    website = coin.get("website") or ""
    has_social = bool(twitter.strip() or telegram.strip() or website.strip())
    if not has_social:
        return False, "no_socials"
    ts_ms = coin.get("created_timestamp")
    if ts_ms:
        hour = time.gmtime(int(ts_ms) // 1000).tm_hour
        if hour in (3, 5):
            return False, f"dead_hour_{hour:02d}UTC"
    return True, "pass"


from hunt.utils.ratelimit import RateLimiter
from hunt.scout.dexscreener import DexScreener
from hunt.watch.price_feed import PriceFeed

FEED: PriceFeed | None = None  # set by price_ws_loop

# real-time exit evaluation state — fed by the price feed on every tick
_last_ws_ts: dict[str, float] = {}
_ws_price: dict[str, float] = {}
# exits v2 — "runner allocation" (the moonshot thesis):
# bank 50% at +40%, 25% at +60%, and the last 25% (moon bag) chases the peak
# with a LADDERED trail that tightens as the multiple grows — wide early so the
# run can breathe, tight late so a monster's top is locked. One USUR (1431x)
# pays for a hundred stop-losses; the ladder must never shake out of the tail.
SL_PCT = -20.0
TRAIL_PCT = 0.15          # ratchet trail between tier 1 and tier 2
TIER_TRIGGERS = [0.40, 0.60]
TIER_FRACS = [0.50, 0.25]
# (peak-multiple floor, trail-from-peak) — first matching row wins
MOON_TRAIL_LADDER = [(0.0, 0.30), (3.0, 0.20), (10.0, 0.12), (50.0, 0.08)]
MCAP_CEILING_SOL = 3000.0  # unused (ceiling commented 2026-09-08 to reopen species-B moonshot hunting)

SOL_USD = 150.0  # last-resort fallback; normally FEED.sol_usd (45s refresh)

_NOTIFIER = None  # telegram alerts, started in run_paper


def _notify(text: str):
    if _NOTIFIER is not None and _NOTIFIER.enabled:
        try:
            import html as _html
            asyncio.get_running_loop().create_task(_NOTIFIER.send(_html.escape(text)))
        except Exception:
            pass


def _sol_usd() -> float:
    if FEED is not None and FEED.sol_usd > 0:
        return FEED.sol_usd
    return SOL_USD if SOL_USD > 0 else 150.0


_fetch_bucket = RateLimiter(1.2, 6)  # shared limiter for pump.fun frontend calls

async def _price_for_mint_fallback(client: httpx.AsyncClient, mint: str) -> float:
    """Price for tokens the curve feed hasn't ticked (graduated/Raydium).
    1) curve feed cache (exact, real-time)  2) GeckoTerminal aggregator.
    DexScreener is intentionally NOT used for paper pricing."""
    if FEED is not None:
        q = FEED.quote(mint) or FEED.stale_quote(mint, max_age_s=300.0)
        if q and q.price_usd > 0:
            return q.price_usd
    try:
        r = await client.get(
            f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}",
            headers={"Accept-Encoding": "gzip"}, timeout=10,
        )
        if r.status_code == 200:
            d = r.json()
            pu = (d.get("data") or {}).get("attributes", {}).get("price_usd")
            if pu:
                return float(pu)
    except: pass
    return 0.0

async def fetch_page(client: httpx.AsyncClient, offset: int, complete: str = "true") -> list[dict]:
    await _fetch_bucket.acquire()
    for attempt in range(3):
        try:
            r = await client.get(
                API,
                params={"offset": offset, "limit": 70, "sort": "created_timestamp", "order": "DESC", "complete": complete},
                headers={"accept": "application/json"},
                timeout=20,
            )
            if r.status_code == 200:
                return r.json() or []
            if r.status_code in (429, 500, 502, 503):
                await asyncio.sleep(1.5 ** attempt)
                continue
            logger.warning("pump page {} failed {}", offset, r.status_code)
            return []
        except Exception as e:
            if attempt == 2:
                logger.warning("pump page {} error {}", offset, e)
                return []
            await asyncio.sleep(1.5 ** attempt)
    return []


def ensure_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(SCHEMA)
    # ensure positions/trades tables exist (from main DB schema)
    from hunt.db.database import SCHEMA as MAIN_SCHEMA
    conn.executescript(MAIN_SCHEMA)
    # add tiered-exit columns if missing
    for col in ("tp_tier INTEGER NOT NULL DEFAULT 0", "realized_sol REAL NOT NULL DEFAULT 0", "decimals INTEGER NOT NULL DEFAULT 6", "mode TEXT NOT NULL DEFAULT 'PAPER'"):
        try:
            conn.execute(f"ALTER TABLE positions ADD COLUMN {col}")
        except Exception:
            pass
    # learning-loop columns (dev reputation + burst shape + holder intel)
    for col in ("dev TEXT", "burst TEXT", "top10 REAL", "snipers INTEGER", "holders INTEGER", "dev_pct REAL"):
        try:
            conn.execute(f"ALTER TABLE {PAPER_DB_TABLE} ADD COLUMN {col}")
        except Exception:
            pass
    conn.commit()
    conn.close()
    REJECT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REJECT_FILE2.parent.mkdir(parents=True, exist_ok=True)


async def _coin_intel(client: httpx.AsyncClient, mint: str) -> dict:
    """in-memory-coin: dev wallet + holder distribution + sniper count.
    Also the only source of the creator address used for dev reputation."""
    try:
        await _fetch_bucket.acquire()
        r = await client.get(f"https://advanced-indexer.pump.fun/in-memory-coin/{mint}", timeout=8)
        if r.status_code == 200:
            d = r.json()
            return {
                "_dev": d.get("dev") or "",
                "_top10": float(d.get("top10HoldersPercent") or 0),
                "_snipers": int(d.get("sniperCount") or 0),
                "_holders": int(d.get("numHolders") or 0),
                "_dev_pct": float(d.get("devHoldingsPercent") or 0),
            }
    except Exception:
        pass
    return {}


async def _socials_for_new_mint(client: httpx.AsyncClient, mint: str) -> dict:
    """pump.fun coins-v3 metadata for a fresh launch (indexed within seconds).
    market_cap from coins-v3 is SOL-denominated for Solana coins (verified:
    MarsMi 165127 vs $16.9M/102) — same units as the /coins dust gate (50 SOL).
    (EVM coins return USD units, but we only process Solana mints.)"""
    try:
        await _fetch_bucket.acquire()
        r = await client.get(f"https://frontend-api-v3.pump.fun/coins-v3/{mint}", timeout=8)
        if r.status_code == 200:
            d = r.json()
            return {
                "twitter": d.get("twitter") or "",
                "telegram": d.get("telegram") or "",
                "website": d.get("website") or "",
                "market_cap": float(d.get("market_cap") or 0) or None,
            }
    except Exception:
        pass
    return {}


async def open_paper_position(mint: str, symbol: str, ds: DexScreener | None = None) -> bool:
    try:
        # ENTRY PRICE: live curve feed first (real-time, exact print from the
        # bonding curve), then aggregators. Never DexScreener for curve entries.
        price_usd = 0.0
        if FEED is not None:
            q = FEED.quote(mint, max_age_s=10.0)
            if q and q.price_usd > 0:
                price_usd = q.price_usd
        if price_usd <= 0 and FEED is not None:
            # subscribe — helius pushes the current curve state immediately
            await FEED.subscribe(mint)
            for _ in range(8):
                await asyncio.sleep(0.25)
                q = FEED.quote(mint, max_age_s=30.0)
                if q and q.price_usd > 0:
                    price_usd = q.price_usd
                    break
        if price_usd <= 0:
            try:
                client = ds.client if ds else httpx.AsyncClient(timeout=10)
                price_usd = await _price_for_mint_fallback(client, mint)
            except: pass
        if price_usd <= 0:
            # fresh coins sometimes don't trade within the first wait window —
            # give them one more chance before dropping an accepted candidate
            await asyncio.sleep(8)
            if FEED is not None:
                q = FEED.quote(mint, max_age_s=30.0)
                if q and q.price_usd > 0:
                    price_usd = q.price_usd
        if price_usd <= 0:
            logger.info("no entry price for {} {} — skipping accepted candidate", mint[:8], symbol)
            return False
        sol_usd = _sol_usd()
        mode = _current_mode()
        from hunt.exec.live import get_live_executor
        ex = get_live_executor()
        if mode == "LIVE" and _LIVE_HALTED:
            conn.close()
            _notify(f"⛔ LIVE DAILY LOSS CAP REACHED — no new live opens until restart/reset")
            return False
        # check existing open
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute("SELECT 1 FROM positions WHERE mint=? AND mode=? AND status='open' LIMIT 1", (mint, mode))
        if cur.fetchone():
            conn.close()
            return False
        # survival tiered max_open
        from hunt.survival.tiers import get_tier
        tier = get_tier()
        max_open = tier.max_open
        if max_open == 0:
            conn.close()
            return False
        cur = conn.execute("SELECT COUNT(*) FROM positions WHERE mode=? AND status='open'", (mode,))
        if (cur.fetchone()[0] or 0) >= max_open:
            conn.close()
            return False
        size_sol = tier.trade_size_sol
        if ex is not None:
            # ---- LIVE: real buy on chain before any DB row exists ----
            if not ex.kp:
                conn.close()
                logger.error("LIVE mode but no wallet — cannot open {}", mint[:8])
                return False
            bal = await ex.balance_sol()
            if bal < size_sol + ex.s.live_min_balance_sol:
                conn.close()
                _notify(f"⛔ LIVE: balance {bal:.3f} SOL < {size_sol}+{ex.s.live_min_balance_sol} reserved — skipping {symbol}")
                logger.warning("LIVE balance too low for {} ({} < {})", symbol, bal, size_sol + ex.s.live_min_balance_sol)
                return False
            r = await ex.buy(mint, size_sol)
            if not r or not r.ok:
                conn.close()
                _notify(f"⛔ LIVE BUY FAILED {symbol} {mint[:6]} — position NOT opened")
                logger.error("LIVE buy failed {}", mint[:8])
                return False
            size_sol = r.sol_lamports / 1e9
            tokens = r.tokens
            decimals = r.decimals
            _LIVE_DECIMALS[mint] = r.decimals
            # anchor to the REAL execution price (fill can be far from the 90s
            # judging snapshot on launch movers — a wrong entry breaks SL/TP/trail)
            base_usd = (size_sol * sol_usd) / tokens if tokens > 0 else price_usd
            logger.info("LIVE open {} {} @${:.6g} size {} SOL venue={} sig={}", mint[:8], symbol, base_usd, size_sol, r.venue, r.signature)
        else:
            decimals = 6
            usd_in = size_sol * sol_usd
            tokens = usd_in / price_usd
            base_usd = price_usd
        # create position
        conn.execute(
            "INSERT INTO positions(opened_ts,mint,symbol,mode,size_sol,tokens,entry_price_usd,tp_pct,sl_pct,trail_pct,peak_price_usd,tp_tier,realized_sol,decimals) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), mint, symbol, mode, size_sol, tokens, base_usd, 100.0, -30.0, 20.0, base_usd, 0, 0.0, decimals),
        )
        conn.execute(
            "INSERT INTO trades(ts,position_id,mode,side,mint,symbol,amount_sol,token_amount,price_usd,status) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), conn.execute("SELECT last_insert_rowid()").fetchone()[0], mode, "BUY", mint, symbol, size_sol, tokens, base_usd, "ok"),
        )
        conn.commit()
        conn.close()
        logger.info("{} open {} {} @ ${:.6g} size {} SOL", mode, mint[:8], symbol, base_usd, size_sol)
        mcap_sol = base_usd / sol_usd * 1e9 if sol_usd > 0 else 0
        _notify(f"🟢 {mode} OPEN {symbol} @{base_usd:.3e} • {size_sol} SOL • mcap ~{mcap_sol:.0f} SOL • {mint[:6]}")
        return True
    except Exception as e:
        logger.warning("open_paper_position fail {}: {}", mint[:8], e)
        return False


async def _process_exit(pos_id: int, mint: str, price: float, sol_usd: float):
    """Tiered scale-out (+40/60/80) with a ratcheting trailing stop. Each TP tier
    locks profit (sells 1/3); once in profit the stop ratchets up so we never give
    a winner back. Hard SL only applies before the first tier is hit.

    LIVE positions execute REAL sells via hunt.exec.live; proceeds come from the
    actual fill (parsed from the confirmed tx). If a live sell fails the position
    is KEPT open (never phantom-closed)."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        pos = conn.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
        if not pos or pos["status"] != "open":
            conn.close(); return
        entry = pos["entry_price_usd"] or price
        if entry <= 0:
            conn.close(); return
        mode = pos["mode"]
        tokens = pos["tokens"]
        size_sol = pos["size_sol"]
        tp_tier = int(pos["tp_tier"] or 0)
        realized = float(pos["realized_sol"] or 0.0)
        peak = max(pos["peak_price_usd"] or entry, price)
        if price > (pos["peak_price_usd"] or 0):
            conn.execute("UPDATE positions SET peak_price_usd=? WHERE id=?", (price, pos_id))
            conn.commit()  # peak MUST persist even when no exit branch fires —
                           # otherwise the trail rebases downward on hot tokens
        change_pct = (price / entry - 1) * 100
        # live executor setup (None in paper mode)
        from hunt.exec.live import get_live_executor
        ex = get_live_executor() if mode == "LIVE" else None
        decimals = int(pos["decimals"] or 6)
        if ex is not None:
            _LIVE_DECIMALS[mint] = decimals

        async def sell_now(units: float) -> tuple:
            """Execute a slice. Returns (proceeds_sol, units_sold).
            * paper: mark-to-market at `price`.
            * live:  REAL sell of `units` from the chain balance (or the whole
                     remaining balance when units == 0); proceeds = actual fill.
            (None, 0) => LIVE sale failed — caller must keep the position open."""
            if ex is None:
                return (units * price / sol_usd) if sol_usd > 0 else 0.0, units
            raw_bal = await ex._token_balance_raw(mint)
            if raw_bal <= 0:
                return 0.0, 0.0
            raw = min(max(1, int(units * 10 ** decimals)), raw_bal) if units > 0 else raw_bal
            r = await ex.sell(mint, raw, close_ata=(raw == raw_bal))
            if not r or not r.ok:
                _notify(f"🆘 SELL FAILED {pos['symbol']} (pos kept open) — tx error, retrying next tick")
                return None, 0.0
            units_sold = (-r.tokens_raw) / 10 ** decimals if r.tokens_raw < 0 else raw / 10 ** decimals
            return r.sol_lamports / 1e9, units_sold

        sold_frac = min(0.50 * tp_tier, 0.75) if tp_tier <= len(TIER_FRACS) else 1.0
        # 1) scale out at each newly crossed TP tier
        while tp_tier < len(TIER_TRIGGERS) and change_pct >= TIER_TRIGGERS[tp_tier] * 100:
            frac = TIER_FRACS[tp_tier]
            sell_units = tokens * (frac / (1.0 - sold_frac)) if sold_frac < 1.0 else tokens
            proceeds, units_sold = await sell_now(sell_units)
            if proceeds is None:
                conn.close(); return
            cost_sold = size_sol * frac
            slice_pnl = proceeds - cost_sold
            realized += slice_pnl
            tokens -= units_sold
            sold_frac += frac
            tp_tier += 1
            conn.execute("UPDATE positions SET tokens=?, tp_tier=?, realized_sol=?, peak_price_usd=? WHERE id=?",
                         (tokens, tp_tier, realized, peak, pos_id))
            conn.commit()
            logger.info("{} tp{} {} +{:.0f}% slice {:+.4f} SOL [ws]", mode, mint[:8], tp_tier, TIER_TRIGGERS[tp_tier - 1] * 100, slice_pnl)
            _notify(f"💰 {mode} TP{tp_tier} {pos['symbol']} +{TIER_TRIGGERS[tp_tier-1]*100:.0f}% slice {slice_pnl:+.4f} SOL")
        # 2) after tier 1 the remaining tranche is protected at breakeven only —
        # a tight trail here would churn out the runner before the moon bag exists
        if tp_tier == 1:
            stop = entry
            if price <= stop:
                proceeds, units_sold = await sell_now(0.0)
                if proceeds is None:
                    conn.close(); return
                cost_rem = size_sol * (1.0 - TIER_FRACS[0])
                realized += proceeds - cost_rem
                conn.execute("UPDATE positions SET status='closed', closed_ts=?, exit_reason='breakeven_stop', exit_sol=?, pnl_sol=? WHERE id=?",
                             (int(time.time()), realized, realized, pos_id))
                conn.commit(); conn.close()
                _notify(f"🔒 {mode} BREAKEVEN STOP {pos['symbol']} {realized:+.4f} SOL")
                return
        # 3) moon bag: laddered trail chases the peak — 30% wide below 3x
        #    (survive the chop), 20% at 3x+, 12% at 10x+, 8% at 50x+
        if tp_tier >= 2:
            peak_mult = peak / entry
            trail_pct = MOON_TRAIL_LADDER[0][1]
            for need, tr in MOON_TRAIL_LADDER:
                if peak_mult >= need:
                    trail_pct = tr
            trail_stop = peak * (1 - trail_pct)
            if price <= trail_stop:
                proceeds, units_sold = await sell_now(0.0)
                if proceeds is None:
                    conn.close(); return
                cost_rem = size_sol * (1.0 - sum(TIER_FRACS))
                realized += proceeds - cost_rem
                conn.execute("UPDATE positions SET status='closed', closed_ts=?, exit_reason='moon_bag_trail', exit_sol=?, pnl_sol=? WHERE id=?",
                             (int(time.time()), realized, realized, pos_id))
                conn.commit(); conn.close()
                _notify(f"🌙 {mode} MOON BAG CLOSED {pos['symbol']} {realized:+.4f} SOL (peak {peak_mult*100:.0f}% of entry, {trail_pct*100:.0f}% trail)")
                return
        # 4) hard SL only before any tier is hit (SL_PCT is already in percent)
        if tp_tier == 0 and change_pct <= SL_PCT:
            proceeds, units_sold = await sell_now(0.0)
            if proceeds is None:
                conn.close(); return
            realized += proceeds - size_sol
            conn.execute("UPDATE positions SET status='closed', closed_ts=?, exit_reason='stop_loss', exit_sol=?, pnl_sol=? WHERE id=?",
                         (int(time.time()), realized, realized, pos_id))
            conn.commit(); conn.close()
            _notify(f"🛑 {mode} SL {pos['symbol']} {realized:+.4f} SOL ({change_pct:.0f}%)")
            return
        conn.close()
    except Exception as e:
        logger.debug("process exit error {}", e)


async def _handle_price_update(mint: str, price_usd: float, sol_usd: float):
    if price_usd <= 0 or not mint: return
    if price_usd < 1e-9 or price_usd > 10:  # sanity filter broken parses
        return
    _last_ws_ts[mint] = time.time()
    _ws_price[mint] = price_usd
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        pos = conn.execute("SELECT id FROM positions WHERE mint=? AND mode IN ('PAPER','LIVE') AND status='open' LIMIT 1", (mint,)).fetchone()
        conn.close()
        if not pos:
            return
        await _process_exit(pos["id"], mint, price_usd, sol_usd)
    except Exception as e:
        logger.debug("ws handle error {}", e)


async def _force_close(pos_id: int, mint: str, price: float, sol_usd: float, reason: str):
    """Sell whatever remains (used for max_hold timeout). LIVE: real sell of the
    full chain balance; keeps the position open if the sell fails."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        pos = conn.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
        if not pos or pos["status"] != "open":
            conn.close(); return
        entry = pos["entry_price_usd"] or price
        mode = pos["mode"]
        tokens = pos["tokens"]
        size_sol = pos["size_sol"]
        tp_tier = int(pos["tp_tier"] or 0)
        realized = float(pos["realized_sol"] or 0.0)
        from hunt.exec.live import get_live_executor
        ex = get_live_executor() if mode == "LIVE" else None
        if ex is not None:
            raw_bal = await ex._token_balance_raw(mint)
            if raw_bal <= 0:
                r = None
                proceeds = 0.0
            else:
                r = await ex.sell(mint, raw_bal)
                if not r or not r.ok:
                    conn.close()
                    _notify(f"🆘 FORCE SELL FAILED {pos['symbol']} ({reason}) — position kept open")
                    logger.error("LIVE force sell failed {} ({})", mint[:8], reason)
                    return
                proceeds = r.sol_lamports / 1e9
        else:
            proceeds = tokens * price / sol_usd if sol_usd > 0 else 0.0
        cost_rem = size_sol * ((2.0 / 3.0) ** tp_tier)
        realized += proceeds - cost_rem
        conn.execute("UPDATE positions SET status='closed', closed_ts=?, exit_reason=?, exit_sol=?, pnl_sol=? WHERE id=?",
                     (int(time.time()), reason, realized, realized, pos_id))
        conn.commit(); conn.close()
        logger.info("{} close {} {} forced {:+.4f} SOL [ws]", mode, mint[:8], reason, realized)
        _notify(f"⏹ {mode} FORCE CLOSE {pos['symbol']} {reason} {realized:+.4f} SOL")
    except Exception as e:
        logger.debug("force close error {}", e)


async def price_ws_loop(stop_event: asyncio.Event):
    """Real-time bonding-curve pricing (Helius accountSubscribe) + SOL/USD refresher.
    Replaces the dead stream.pumpapi.io websocket that never delivered a tick."""
    global FEED
    FEED = PriceFeed(on_tick=lambda mint, q: _handle_price_update(mint, q.price_usd, _sol_usd()))
    await FEED.run(stop_event)


async def paper_stops_loop(stop_event: asyncio.Event):
    global _LIVE_HALTED
    # ws is the real-time pricing/exit engine for bonding-curve tokens.
    # This poll is a fallback for mints the ws hasn't ticked (graduated/Raydium),
    # using GeckoTerminal, every 3s.
    ws_task = asyncio.create_task(price_ws_loop(stop_event))
    client = httpx.AsyncClient(timeout=10)
    while not stop_event.is_set():
        try:
            now = time.time()
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            positions = conn.execute("SELECT * FROM positions WHERE mode IN ('PAPER','LIVE') AND status='open'").fetchall()
            conn.close()
            # KILL FILE (live safety): emergency close every open LIVE position.
            # `touch hunt/data/kill_live` on AWS = force-sell + exit now.
            try:
                from hunt.config import get_settings
                if os.path.exists(os.path.abspath(get_settings().kill_file)):
                    logger.warning("KILL FILE DETECTED — force-closing all LIVE positions")
                    for pos in list(positions):
                        if pos["mode"] == "LIVE":
                            await _force_close(pos["id"], pos["mint"], pos["peak_price_usd"] or pos["entry_price_usd"], _sol_usd(), "kill")
                    positions = [p for p in positions if p["mode"] != "LIVE"]
                    os.remove(os.path.abspath(get_settings().kill_file))
                    _notify("🛑 KILL FILE processed — all LIVE positions closed. Restart to resume.")
            except Exception as e:
                logger.debug("kill file error {}", e)
            # daily-loss cap (UTC day): realized live losses beyond the cap => halt
            try:
                if _current_mode() == "LIVE" and not _LIVE_HALTED:
                    import datetime as _dt
                    day_start = int(_dt.datetime.now(_dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
                    conn = sqlite3.connect(DB_PATH)
                    lost = conn.execute("SELECT COALESCE(SUM(pnl_sol),0) FROM positions WHERE mode='LIVE' AND status='closed' AND closed_ts>=?", (day_start,)).fetchone()[0]
                    conn.close()
                    if float(lost) < -get_settings().live_daily_loss_cap_sol:
                        _LIVE_HALTED = True
                        _notify(f"🛑 LIVE DAILY LOSS CAP — {lost:+.3f} SOL today. Force-closing ALL live positions.")
                        for pos in list(positions):
                            if pos["mode"] == "LIVE":
                                await _force_close(pos["id"], pos["mint"], pos["peak_price_usd"] or pos["entry_price_usd"], _sol_usd(), "daily_loss_cap")
                        positions = [p for p in positions if p["mode"] != "LIVE"]
            except Exception as e:
                logger.debug("daily loss cap error {}", e)
            # keep feed subscriptions in sync with open positions (self-healing
            # after reconnects; unsubscribes closed positions automatically)
            if FEED is not None:
                open_mints = {p["mint"] for p in positions}
                for m in open_mints:
                    await FEED.subscribe(m)
                for m in await FEED.subscribed_mints():
                    if m not in open_mints:
                        await FEED.unsubscribe(m)
            # only poll mints not recently covered by ws
            uncovered = [p for p in positions if now - _last_ws_ts.get(p["mint"], 0) > 3.0]
            for pos in uncovered:
                price = await _price_for_mint_fallback(client, pos["mint"])
                if price <= 0: continue
                if price > (pos["peak_price_usd"] or 0):
                    conn = sqlite3.connect(DB_PATH)
                    conn.execute("UPDATE positions SET peak_price_usd=? WHERE id=?", (price, pos["id"]))
                    conn.commit(); conn.close()
                # tiered exit handles SL/TP/trailing internally
                await _process_exit(pos["id"], pos["mint"], price, _sol_usd())
            # max_hold: 6h for normal positions; 24h for moon bags (tp_tier>=2
            # means cost basis is banked — house money rides, the trail protects it)
            for pos in positions:
                hold_cap = 24*3600 if int(pos["tp_tier"] or 0) >= 2 else 6*3600
                if now - pos["opened_ts"] > hold_cap:
                    price = await _price_for_mint_fallback(client, pos["mint"])
                    if price <= 0: price = pos["peak_price_usd"] or pos["entry_price_usd"]
                    await _force_close(pos["id"], pos["mint"], price, _sol_usd(), "max_hold")
        except Exception as e:
            logger.debug("paper stops poll error {}", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            pass
    ws_task.cancel()
    try: await ws_task
    except: pass
    try: await client.aclose()
    except: pass


async def _handle_candidate(coin: dict, client: httpx.AsyncClient, ds: DexScreener,
                            stats: dict) -> None:
    """Full gate chain for one candidate: survival filter → smart-wallet boost →
    risk gate → paper open/reject. Shared by the ws discovery stream and the
    safety-net poll."""
    mint = coin["mint"]
    symbol = coin.get("symbol") or "?"
    # holder/dev intel for EVERY decision path (stream waitlist AND safety poll)
    # — the learning set: winners vs losers launch fingerprints live in these fields
    if "_top10" not in coin:
        intel = await _coin_intel(client, mint)
        if intel:
            coin.update(intel)
    accept, reason = survival_filter(coin)
    created_ts = int(coin.get("created_timestamp") or 0) // 1000
    now = int(time.time())
    dev = coin.get("_dev") or None

    # dev reputation from our own launch history (self-learning, passive):
    # a creator whose prior launches all stop-lossed fast and never ran gets
    # vetoed; a creator with a proven runner gets annotated for the digest
    if dev and accept:
        try:
            conn = sqlite3.connect(DB_PATH)
            prior = conn.execute(
                """SELECT COUNT(DISTINCT pd.mint) n,
                          SUM(CASE WHEN po.pnl_sol > 0.5 THEN 1 ELSE 0 END) runners,
                          SUM(CASE WHEN po.exit_reason='stop_loss' AND (po.closed_ts-po.opened_ts) < 900 THEN 1 ELSE 0 END) fast_rugs
                   FROM paper_decisions pd
                   JOIN positions po ON po.mint=pd.mint AND po.mode='PAPER' AND po.status='closed'
                   WHERE pd.dev=? AND pd.mint != ?""", (dev, mint)).fetchone()
            conn.close()
            if prior and (prior["n"] or 0) >= 2 and (prior["runners"] or 0) == 0 and (prior["fast_rugs"] or 0) >= 2:
                accept, reason = False, f"serial_rugger_{prior['fast_rugs']}"
            elif prior and (prior["runners"] or 0) >= 1:
                reason = f"{reason}+dev_winner"
        except Exception:
            pass

    # smart wallet boost (automaton replication lineage: if tracked wallet bought, boost)
    smart_reason = ""
    try:
        from hunt.utils.smart_wallet import check_smart_buy
        is_smart, who = await check_smart_buy(mint)
        if is_smart and not accept:
            # boost: if survival_filter rejected but smart wallet bought, flip to accept
            accept = True
            reason = f"smart_boost_{who}"
            smart_reason = who
        elif is_smart:
            reason = f"{reason}+smart_{who}"
    except: pass
    # SolanaTracker risk gate (zostaff style: risk>7 veto, top10≥80%, dev≥25%)
    tracker_reason = ""
    if accept:
        try:
            from hunt.utils.solanatracker import check_risk
            ok_risk, tracker_reason = await check_risk(mint, client)
            if not ok_risk:
                # but if smart wallet, don't veto on risk alone (let it ride)
                if smart_reason:
                    tracker_reason = f"risk_{tracker_reason}_overridden_by_smart"
                else:
                    accept = False
                    reason = tracker_reason
        except Exception:
            pass

    conn = sqlite3.connect(DB_PATH)
    conn.execute(SCHEMA)
    conn.execute(
        f"INSERT OR IGNORE INTO {PAPER_DB_TABLE} (mint,symbol,created_ts,decision,reason,twitter,telegram,website,market_cap,decided_at,dev,burst,top10,snipers,holders,dev_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mint, symbol, created_ts, "ACCEPT" if accept else "REJECT", reason,
         coin.get("twitter"), coin.get("telegram"), coin.get("website"),
         float(coin.get("market_cap") or 0), now, dev, coin.get("_burst"),
         coin.get("_top10"), coin.get("_snipers"), coin.get("_holders"), coin.get("_dev_pct")),
    )
    conn.commit()
    conn.close()
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now))} | {mint} | {symbol} | {'ACCEPT' if accept else 'REJECT'} | {reason}{'/'+tracker_reason if tracker_reason and tracker_reason!='risk_'+tracker_reason else ''} | twitter={bool(coin.get('twitter'))} website={bool(coin.get('website'))} hour={time.gmtime(created_ts).tm_hour if created_ts else '?'}UTC"
    if accept:
        stats["accepted"] += 1
        logger.info("ACCEPT {} {} ({}) [{}]", mint[:8], symbol, reason, tracker_reason)
        try:
            ok = await open_paper_position(mint, symbol, ds)
            if ok: stats["opened"] += 1
        except Exception as e:
            logger.debug("open position failed {}: {}", mint[:8], e)
    else:
        stats["rejected"] += 1
        # append to both a.txt files
        for p in (REJECT_FILE, REJECT_FILE2):
            with open(p, "a") as f:
                f.write(line + "\n")
        logger.info("REJECT {} {} ({})", mint[:8], symbol, reason)


async def run_paper(duration_s: int = 3600, poll_interval_s: int = 30) -> dict:
    global _NOTIFIER
    ensure_db()
    from hunt.config import get_settings
    from hunt.notify.base import Notifier
    _s = get_settings()
    if _s.telegram_bot_token and _s.telegram_chat_id:
        _NOTIFIER = Notifier(_s.telegram_bot_token, _s.telegram_chat_id)
        _NOTIFIER.start()
    live = not _s.dry_run
    if live:
        # ---- LIVE preflight: fail closed if the wallet/config is not ready ----
        from hunt.exec.live import get_live_executor
        ex = get_live_executor()
        if ex is None or not ex.kp:
            _notify(f"🔴 FAILED TO START LIVE — HUNT_WALLET_PRIVATE_KEY required when HUNT_DRY_RUN=false (bal check skipped)")
            logger.critical("LIVE mode requires HUNT_WALLET_PRIVATE_KEY")
            raise SystemExit("LIVE mode requires HUNT_WALLET_PRIVATE_KEY")
        bal = await ex.balance_sol()
        if bal < _s.live_min_balance_sol:
            _notify(f"🔴 FAILED TO START LIVE — wallet {ex.wallet} balance {bal:.4f} SOL < min {_s.live_min_balance_sol} SOL")
            logger.critical("LIVE balance too low: {} < {}", bal, _s.live_min_balance_sol)
            raise SystemExit("LIVE wallet balance too low")
        _notify(f"🔥 LIVE TRADING STARTED — {ex.wallet[:8]}… {bal:.3f} SOL • {duration_s//60} min window")
    else:
        _notify(f"🤖 hunt paper run started — {duration_s//60} min window")
    start = time.time()
    end = start + duration_s
    seen: set[str] = set()
    # preload seen from DB
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(f"SELECT mint FROM {PAPER_DB_TABLE}").fetchall()
        for (m,) in rows:
            seen.add(m)
    except Exception:
        pass
    # also preload from pump_grads to avoid re-processing old history
    try:
        rows = conn.execute("SELECT mint FROM pump_grads").fetchall()
        for (m,) in rows:
            seen.add(m)
    except Exception:
        pass
    conn.close()

    stats = {"accepted": 0, "rejected": 0, "total_scanned": 0, "opened": 0}
    queue: asyncio.Queue = asyncio.Queue(maxsize=400)
    # coins wait here until they're old enough to judge (every pump.fun coin is
    # born at ~28 SOL mcap; the tuned dust gate only makes sense ~90s in, using
    # the live curve mcap at decision time)
    WAITLIST_MIN_AGE_S = 90.0
    waitlist: list[dict] = []

    async def on_new_token(evt: dict):
        """PumpPortal new-launch event → candidate. Sub-second discovery."""
        mint = evt.get("mint") or ""
        if not mint or mint in seen or not mint.endswith("pump"):
            return
        seen.add(mint)
        stats["total_scanned"] += 1
        coin = {
            "mint": mint,
            "symbol": evt.get("symbol") or "?",
            "name": evt.get("name") or "",
            "market_cap": float(evt.get("marketCapSol") or 0),  # SOL units at birth
            "created_timestamp": int(time.time() * 1000),
            "_from_ws": True,
        }
        try:
            queue.put_nowait(coin)
        except asyncio.QueueFull:
            # drop the oldest, keep the freshest — moonshots don't come from backlogs
            try:
                queue.get_nowait(); queue.put_nowait(coin)
            except Exception:
                pass

    # start pricing/exit engine + heartbeat + discovery stream
    from hunt.heartbeat.monitor import heartbeat_loop
    from hunt.watch.discovery_ws import new_tokens_loop
    stop_evt = asyncio.Event()
    stops_task = asyncio.create_task(paper_stops_loop(stop_evt))
    hb_task = asyncio.create_task(heartbeat_loop(stop_evt, 60))
    disc_task = asyncio.create_task(new_tokens_loop(stop_evt, on_new_token))
    logger.info("[paper] event-driven discovery active (pumpportal stream) + 60s safety poll")

    async with httpx.AsyncClient(timeout=20) as client:
        ds_for_open = DexScreener(client)
        last_safety_poll = 0.0
        while time.time() < end:
            now_ms = time.time() * 1000
            # safety-net poll runs on its own 60s clock even when the waitlist
            # is busy — the idle-only version starved during launch bursts and
            # let outage-missed launches go completely unseen
            do_safety = time.time() - last_safety_poll >= 60.0
            if do_safety:
                last_safety_poll = time.time()
            # drain the ws queue into the waitlist (non-blocking)
            while True:
                try:
                    waitlist.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if len(waitlist) > 500:  # safety: oldest coins expire, keep newest
                waitlist[:] = waitlist[-500:]
            # process coins that reached decision age
            due = [c for c in waitlist
                   if now_ms - int(c.get("created_timestamp") or 0) >= WAITLIST_MIN_AGE_S * 1000]
            if do_safety:
                try:
                    pages = await fetch_page(client, 0, "false"), await fetch_page(client, 0, "true")
                    for c in pages[0] + pages[1]:
                        m = c.get("mint")
                        if not m or m in seen:
                            continue
                        seen.add(m)
                        stats["total_scanned"] += 1
                        await _handle_candidate(c, client, ds_for_open, stats)
                except Exception as e:
                    logger.debug("safety poll error {}", e)
            if due:
                waitlist = [c for c in waitlist if c not in due]
            elif waitlist:
                # coins maturing toward decision age — wait, don't busy-spin
                await asyncio.sleep(2.0)
                continue
            else:
                try:
                    coin = await asyncio.wait_for(queue.get(), timeout=5.0)
                    waitlist.append(coin)
                except asyncio.TimeoutError:
                    pass
                continue
            for coin in due:
                try:
                    # cheap enrichment first (socials + SOL-unit mcap from coins-v3)
                    socials = await _socials_for_new_mint(client, coin["mint"])
                    if socials:
                        coin.update(socials)
                    if FEED is not None:
                        q = FEED.quote(coin["mint"], max_age_s=20.0)
                        if q and q.mcap_sol > 0:
                            coin["market_cap"] = q.mcap_sol
                    if not coin.get("market_cap"):
                        coin["market_cap"] = 0.0
                    # burst sampling in two bands: [50, 3000] = entry
                    # candidates; [3000, 100000] = Species-B observation
                    # (instant-mega launches — log-only, ceiling still vetoes)
                    mc = float(coin.get("market_cap") or 0)
                    if 50.0 <= mc <= 100000.0:
                        burst = {}
                        if FEED is not None:
                            q0 = FEED.stale_quote(coin["mint"], 1e9)
                            t0 = FEED.ticks_received
                            await FEED.subscribe(coin["mint"])
                            await asyncio.sleep(2.5)
                            q1 = FEED.quote(coin["mint"], 600)
                            if q1:
                                burst = {"ticks": FEED.ticks_received - t0,
                                         "mcap_delta_pct": round((q1.mcap_sol / q0.mcap_sol - 1) * 100, 1) if q0 and q0.mcap_sol else None,
                                         "graduated": q1.graduated}
                                if q1.mcap_sol > 0:
                                    coin["market_cap"] = q1.mcap_sol
                        coin["_burst"] = json.dumps(burst) if burst else None
                    await _handle_candidate(coin, client, ds_for_open, stats)
                except Exception as e:
                    logger.exception("candidate error: {}", e)

            elapsed = int(time.time() - start)
            logger.info("paper #{}s: scanned={} accepted={} rejected={} opened={} queue={} | elapsed {}s remaining {}s",
                        int(time.time() - start), stats["total_scanned"], stats["accepted"], stats["rejected"], stats["opened"], queue.qsize(), elapsed, max(0, int(end - time.time())))

            # survival tiered cadence caps how hot the gate chain runs
            from hunt.survival.tiers import get_tier
            tier = get_tier()
            if tier.name == "dead":
                logger.warning("DEAD tier — paper halted, waiting for manual fund")
                await asyncio.sleep(min(300, max(0, end - time.time())))

    stop_evt.set()
    for t in (hb_task, disc_task, stops_task):
        try: t.cancel()
        except: pass
    try:
        await stops_task
    except: pass
    # final stats
    try:
        conn = sqlite3.connect(DB_PATH)
        open_n = conn.execute("SELECT COUNT(*) FROM positions WHERE mode IN ('PAPER','LIVE') AND status='open'").fetchone()[0]
        closed = conn.execute("SELECT COUNT(*), COALESCE(SUM(pnl_sol),0) FROM positions WHERE mode IN ('PAPER','LIVE') AND status='closed' AND opened_ts>=?", (int(start),)).fetchone()
        conn.close()
        logger.info("{} positions: open={} closed={} pnl={:+.4f} SOL (opened this run: {})", _current_mode(), open_n, closed[0], closed[1], stats["opened"])
    except: pass
    logger.info("PAPER RUN COMPLETE: duration={}s accepted={} rejected={} total_scanned={} opened_positions={}", int(time.time()-start), stats["accepted"], stats["rejected"], stats["total_scanned"], stats["opened"])
    return {"accepted": stats["accepted"], "rejected": stats["rejected"], "total_scanned": stats["total_scanned"], "opened_positions": stats["opened"], "duration_s": int(time.time()-start)}


if __name__ == "__main__":
    import sys
    from hunt.config import get_settings
    from hunt.log import setup_logging
    setup_logging(get_settings().log_level)
    dur = int(sys.argv[1]) if len(sys.argv) > 1 else 3600
    asyncio.run(run_paper(duration_s=dur))
