from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

import httpx
from loguru import logger

from hunt.config import get_settings

DB_PATH = "hunt/data/hunt.sqlite3"
API = "https://frontend-api-v3.pump.fun/coins"
REJECT_FILE = Path("a.txt")
REJECT_FILE2 = Path("hunt/data/paper_rejected.txt")
PAPER_DB_TABLE = "paper_decisions"

_LIVE_DECIMALS: dict[str, int] = {}
_LIVE_HALTED = False  # set after hitting the daily loss cap — blocks new live opens
_RUN_END_TS = 0.0  # run_paper window end; no new opens past it (soft-bound guard)
# exit-pricing state (LIVE): mint -> (ts, source); blindness alert throttle
_px_seen: dict[str, tuple[float, str]] = {}
_blind_warned: dict[str, float] = {}
_jup_px_cache: dict[str, tuple[float, float]] = {}  # mint -> (ts, price)


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
    dev_pct REAL,
    tracker_json TEXT
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
    # 2b. MCAP: curve FDV or PumpSwap vault FDV. API market_cap is log-only.
    # Curve (species-A): 50–3000 SOL. Graduated AMM (species-B): ≥50 SOL, no ceiling.
    if not coin.get("_mcap_ok"):
        return False, "mcap_unavailable"
    try:
        mc = float(coin.get("market_cap") or 0)
    except (TypeError, ValueError):
        return False, "mcap_unavailable"
    if mc < 50:
        return False, f"dust_mcap_{mc:.0f}"
    if not coin.get("_graduated") and mc > MCAP_CEILING_SOL:
        return False, f"mcap_ceiling_{mc:.0f}"
    # 2e. top-10 of CIRCULATING supply (curve ATA excluded). Indexer zeros are
    # not a pass. Missing measurement is a REJECT. Snipers are not gated here —
    # getTokenLargestAccounts cannot see them; indexer sniperCount is the same
    # feed that returned false-clean on Rufus.
    if not coin.get("_intel_ok"):
        return False, "intel_unavailable"
    try:
        top10 = float(coin["_top10"])
    except (TypeError, ValueError, KeyError):
        return False, "intel_unavailable"
    if top10 > 75:
        return False, f"top10_heavy_{top10:.0f}"
    # 2c. DEMAND GATE: API last_trade_timestamp is the same unreliable feed as
    # market_cap. Skip it when on-chain FDV already proves the curve is live
    # (BERR 2026-09-11: $12.7K / 32.7% top10 killed by a 207s API stamp).
    try:
        ltt = coin.get("last_trade_timestamp")
        mc = float(coin.get("market_cap") or 0)
        if ltt and mc < 50:
            stale_s = (time.time()*1000 - float(ltt)) / 1000.0
            if stale_s > 180.0:
                return False, f"stale_no_trade_{stale_s:.0f}s"
    except (TypeError, ValueError):
        pass
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
MCAP_CEILING_SOL = 3000.0  # RE-ENABLED 2026-09-11 (operator order): ≤3000 SOL, species-A only
                           # for the supervised live session; species-B stays out via mcap_ceiling.

_NOTIFIER = None  # telegram alerts, started in run_paper
_fx_ts = 0.0
_fx_px = 0.0


def _notify(text: str, *, sol: float | None = None):
    """SOL is the mark. Optional `sol=` fetches FX once at send time for a $ tag."""
    if _NOTIFIER is None or not _NOTIFIER.enabled:
        return

    async def _go():
        msg = text
        if sol is not None:
            try:
                fx = await _fx_usd_for_notify()
                if fx > 0:
                    msg = f"{text} (~${sol * fx:.2f})"
            except Exception:
                pass
        try:
            import html as _html
            await _NOTIFIER.send(_html.escape(msg))
        except Exception:
            pass

    try:
        asyncio.get_running_loop().create_task(_go())
    except Exception:
        pass


async def _fx_usd_for_notify() -> float:
    """On-demand SOL/USD for Telegram only. Never written onto the price feed."""
    global _fx_ts, _fx_px
    now = time.time()
    if _fx_px > 0 and now - _fx_ts < 60:
        return _fx_px
    try:
        async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "hunt/1"}) as c:
            r = await c.get(
                "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112"
            )
            px = float(((r.json().get("pairs") or [{}])[0] or {}).get("priceUsd") or 0)
            if px > 0:
                _fx_ts, _fx_px = now, px
                return px
    except Exception:
        pass
    return _fx_px if _fx_px > 0 else 0.0


def _entry_sol(pos) -> float:
    try:
        v = pos["entry_price_sol"]
        if v and float(v) > 0:
            return float(v)
    except (KeyError, IndexError, TypeError):
        pass
    tokens = float(pos["tokens"] or 0)
    size = float(pos["size_sol"] or 0)
    return (size / tokens) if tokens > 0 else 0.0


def _peak_sol(pos, entry: float) -> float:
    try:
        v = pos["peak_price_sol"]
        if v and float(v) > 0:
            return float(v)
    except (KeyError, IndexError, TypeError):
        pass
    return entry


_fetch_bucket = RateLimiter(1.2, 6)  # shared limiter for pump.fun frontend calls

async def _jup_entry_price(client: httpx.AsyncClient, mint: str, sol_usd: float) -> float:
    """Entry-price fallback for graduated coins under an invisible aggregator
    (Kekius/SUPERCYCLE/Job 09-10): probe a Jupiter buy route. If a route exists
    for WSOL→mint, the pool IS trading and its price is the entry price — and
    the actual live buy uses that same route, so price and fill agree.
    Returns USD price per token, or 0.0 if no route/no decimals."""
    try:
        from hunt.exec.jupiter import JupiterClient
        from hunt.config import WSOL
        from hunt.exec.live import get_live_executor
        jup = JupiterClient(client)
        q = await jup.quote(WSOL, mint, 10_000_000)  # 0.01 SOL probe
        if not q or q.out_amount_raw <= 0 or q.in_amount_raw <= 0:
            return 0.0
        ex_probe = get_live_executor()
        dec = int(await ex_probe._token_decimals(mint)) if ex_probe else 6
        ui = q.out_amount_raw / (10 ** dec)
        if ui <= 0:
            return 0.0
        return (q.in_amount_raw / 1e9) * sol_usd / ui
    except Exception:
        return 0.0


async def _curve_reserve_entry_price(
    ex, client: httpx.AsyncClient, mint: str, sol_usd: float,
) -> float:
    """Deterministic entry price from the on-chain bonding-curve reserves.

    Constant-product price = quote_out / token_out for the curve, converted
    to SOL-equivalent via a Jupiter SOL->quote probe route (routes exist for
    every trading quote mint — verified 2026-09-10). Needs no RPC stream, so
    it prices un-graduated coins the feed is blind to.

    Returns USD per token, or 0.0 when the curve/reserves are unreadable.
    """
    try:
        import struct as _st
        import base64 as _b64

        from hunt.exec.pumpfun.pda import get_bonding_curve_pda
        from solders.pubkey import Pubkey
        from hunt.exec.jupiter import JupiterClient
        from hunt.config import WSOL

        curve = get_bonding_curve_pda(Pubkey.from_string(mint))
        result = await ex._raw_rpc("getAccountInfo", [str(curve), {"encoding": "base64"}])
        value = (result or {}).get("value")
        if not value:
            return 0.0
        data = _b64.b64decode(value["data"][0])
        if len(data) < 49:
            return 0.0
        v_token = _st.unpack_from("<Q", data, 8)[0]
        v_quote = _st.unpack_from("<Q", data, 16)[0]
        if v_token <= 0 or v_quote <= 0:
            return 0.0
        # quote SOL value: WSOL = 1:1; USDC = 1e6/1e9 SOL-ish via price; else probe
        quote_mint = WSOL
        if len(data) >= 115 and data[83:115] != b"\x00" * 32:
            quote_mint = str(Pubkey(data[83:115]))
        quote_sol_usd = sol_usd  # per 1 SOL of quote == $sol_usd for WSOL
        if quote_mint != WSOL:
            jup = JupiterClient(client)
            # price: how much SOL is 1 quote token worth
            qq = await jup.quote(quote_mint, WSOL, 10_000_000)
            if qq and qq.out_amount_raw > 0:
                quote_sol_usd = (qq.out_amount_raw / 1e9) * sol_usd / (10_000_000 / 1e6)
            else:
                return 0.0
        # constant-product: token_USD = (quote/token) * quote_SOL_usd
        token_usd = (v_quote / v_token) * quote_sol_usd
        return token_usd if token_usd > 0 else 0.0
    except Exception:
        return 0.0

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
    for col in ("tp_tier INTEGER NOT NULL DEFAULT 0", "realized_sol REAL NOT NULL DEFAULT 0", "decimals INTEGER NOT NULL DEFAULT 6", "mode TEXT NOT NULL DEFAULT 'PAPER'",
                "entry_price_sol REAL", "peak_price_sol REAL"):
        try:
            conn.execute(f"ALTER TABLE positions ADD COLUMN {col}")
        except Exception:
            pass
    for r in conn.execute("SELECT id, size_sol, tokens, entry_price_usd, peak_price_usd FROM positions WHERE status='open' AND (entry_price_sol IS NULL OR entry_price_sol=0)"):
        pid, size, tokens, e_usd, p_usd = r
        if tokens and tokens > 0 and size:
            entry_sol = float(size) / float(tokens)
            peak_sol = entry_sol
            if e_usd and float(e_usd) > 0 and p_usd:
                peak_sol = entry_sol * (float(p_usd) / float(e_usd))
            conn.execute("UPDATE positions SET entry_price_sol=?, peak_price_sol=? WHERE id=?",
                         (entry_sol, peak_sol, pid))
    # learning-loop columns (dev reputation + burst shape + holder intel)
    for col in ("dev TEXT", "burst TEXT", "top10 REAL", "snipers INTEGER", "holders INTEGER", "dev_pct REAL", "tracker_json TEXT"):
        try:
            conn.execute(f"ALTER TABLE {PAPER_DB_TABLE} ADD COLUMN {col}")
        except Exception:
            pass
    conn.commit()
    conn.close()
    REJECT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REJECT_FILE2.parent.mkdir(parents=True, exist_ok=True)


async def _coin_intel(client: httpx.AsyncClient, mint: str) -> dict:
    """On-chain circulating top-10 is the gate. Indexer is _dev only (reputation).

    Never coerce a missing measurement to 0 — that was the Rufus false-clean.
    """
    intel: dict = {"_intel_ok": False, "_ocr": False, "_dev": ""}
    try:
        await _fetch_bucket.acquire()
        r = await client.get(f"https://advanced-indexer.pump.fun/in-memory-coin/{mint}", timeout=8)
        if r.status_code == 200:
            d = r.json()
            intel["_dev"] = d.get("dev") or ""
            intel["_idx_top10"] = float(d.get("top10HoldersPercent") or 0)
            intel["_snipers"] = int(d.get("sniperCount") or 0)
            intel["_holders"] = int(d.get("numHolders") or 0)
            intel["_dev_pct"] = float(d.get("devHoldingsPercent") or 0)
    except Exception:
        pass
    try:
        from hunt.paper.onchain_intel import fetch_onchain_top10
        pct = await fetch_onchain_top10(client, get_settings().rpc_http, mint)
        if pct is None:
            return intel
        intel["_top10"] = pct
        intel["_intel_ok"] = True
        intel["_ocr"] = True
        logger.info("intel on-chain {} top10={:.1f}% (circulating, curve excluded)", mint[:8], pct)
    except Exception as e:
        logger.debug("on-chain intel failed {}: {}", mint[:8], e)
    return intel


def _fdv_from_payload(coin: dict) -> tuple[float | None, bool] | None:
    """FDV from /coins virtual reserves when present. Same formula as on-chain."""
    try:
        if coin.get("complete") is True:
            return None, True
        vs = int(coin.get("virtual_sol_reserves") or 0)
        vt = int(coin.get("virtual_token_reserves") or 0)
        supply = int(coin.get("total_supply") or coin.get("token_total_supply") or 0)
        if vs <= 0 or vt <= 0 or supply <= 0:
            return None
        from hunt.paper.onchain_intel import curve_fdv_sol
        return curve_fdv_sol(vs, vt, supply), False
    except (TypeError, ValueError):
        return None


async def _onchain_mcap(client: httpx.AsyncClient, mint: str) -> tuple[float | None, bool] | None:
    """On-chain curve FDV in SOL. None = unreadable. graduated=True → FDV is None."""
    try:
        from hunt.paper.onchain_intel import fetch_curve_mcap
        return await fetch_curve_mcap(client, get_settings().rpc_http, mint)
    except Exception:
        return None


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
        logger.info("opening {} {}", mint[:8], symbol)
        price_sol = 0.0
        client = ds.client if ds else httpx.AsyncClient(timeout=10)
        if FEED is not None:
            try:
                q = await asyncio.wait_for(FEED.ensure_priced(mint), timeout=8.0)
            except asyncio.TimeoutError:
                logger.warning("ensure_priced timeout {} {}", mint[:8], symbol)
                q = None
            if q and q.price_sol > 0:
                price_sol = q.price_sol
            if price_sol <= 0:
                await asyncio.sleep(0.6)
                q2 = FEED.stale_quote(mint, 30.0) or FEED.quote(mint, 30.0)
                if q2 and q2.price_sol > 0:
                    price_sol = q2.price_sol
        if price_sol <= 0:
            _notify(f"⚠️ NO ENTRY PRICE {symbol} {mint[:8]} — gated ACCEPT but Helius blind, skipped")
            logger.info("no entry price for {} {} — skipping accepted candidate", mint[:8], symbol)
            return False
        mode = _current_mode()
        from hunt.exec.live import get_live_executor
        ex = get_live_executor()
        # operator pause (Telegram /pause or pause file): no new opens at all.
        from hunt.config import get_settings as _gs
        if os.path.exists(os.path.abspath(_gs().pause_file)):
            logger.info("paused — skipping {} {}", mint[:8], symbol)
            return False
        # window discipline: the soft-bound overrun must never OPEN past end
        # (eL5f opened 7 min after its window ended, then sat unmanaged).
        if _RUN_END_TS and time.time() > _RUN_END_TS:
            logger.info("past window end — no new opens {} {}", mint[:8], symbol)
            return False
        if mode == "LIVE" and _LIVE_HALTED:
            _notify(f"⛔ LIVE DAILY LOSS CAP REACHED — no new live opens until restart/reset")
            return False
        # check existing open
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cur = conn.execute("SELECT 1 FROM positions WHERE mint=? AND mode=? AND status='open' LIMIT 1", (mint, mode))
        if cur.fetchone():
            conn.close()
            logger.info("already open {} {}", mint[:8], symbol)
            return False
        from hunt.survival.tiers import get_tier
        tier = get_tier()
        max_open = tier.max_open
        if max_open == 0:
            conn.close()
            logger.info("max_open=0 skip {} {}", mint[:8], symbol)
            return False
        cur = conn.execute("SELECT COUNT(*) FROM positions WHERE mode=? AND status='open'", (mode,))
        nopen = cur.fetchone()[0] or 0
        if nopen >= max_open:
            conn.close()
            logger.info("at cap {}/{} skip {} {}", nopen, max_open, mint[:8], symbol)
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
            base_sol = size_sol / tokens if tokens > 0 else price_sol
            # PAYUP: fill price_sol vs Helius pool tick. No FX / Dex USD.
            pool_sol = 0.0
            if FEED is not None:
                qpay = FEED.stale_quote(mint, 60.0)
                pool_sol = qpay.price_sol if qpay else 0.0
            if pool_sol > 0 and base_sol > 0:
                from hunt.config import get_settings as _gs5
                over = base_sol / pool_sol - 1.0
                if over > float(_gs5().live_entry_payup_guard_pct or 15) / 100:
                    logger.warning("payup guard reversing {} fill {:.3e} {:.0f}% over helius {:.3e}",
                                   mint[:8], base_sol, over * 100, pool_sol)
                    _notify(f"🛟 PAYUP GUARD {symbol}: fill {over*100:.0f}% over Helius — reversing, no position")
                    rb = await ex.sell(mint, int(r.tokens_raw))
                    if rb and rb.ok:
                        conn.close()
                        _notify(f"🛟 PAYUP reversed {symbol}: got {rb.sol_lamports/1e9:.4f} SOL")
                        return False
                    logger.error("payup reverse failed — recording managed position {}", mint[:8])
                    _notify(f"🛟 PAYUP GUARD {symbol}: reverse FAILED, position recorded (SL manages it)")
            else:
                logger.info("payup guard skipped (no Helius tick) {}", mint[:8])
            logger.info("LIVE open {} {} @{} SOL/tok size {} SOL venue={} sig={}", mint[:8], symbol, base_sol, size_sol, r.venue, r.signature)
        else:
            decimals = 6
            tokens = size_sol / price_sol
            base_sol = price_sol
        conn.execute(
            "INSERT INTO positions(opened_ts,mint,symbol,mode,size_sol,tokens,entry_price_usd,tp_pct,sl_pct,trail_pct,peak_price_usd,tp_tier,realized_sol,decimals,entry_price_sol,peak_price_sol) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), mint, symbol, mode, size_sol, tokens, 0.0, 100.0, SL_PCT, 20.0, 0.0, 0, 0.0, decimals, base_sol, base_sol),
        )
        conn.execute(
            "INSERT INTO trades(ts,position_id,mode,side,mint,symbol,amount_sol,token_amount,price_usd,status) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), conn.execute("SELECT last_insert_rowid()").fetchone()[0], mode, "BUY", mint, symbol, size_sol, tokens, 0.0, "ok"),
        )
        conn.commit()
        conn.close()
        logger.info("{} open {} {} @ {:.6g} SOL/tok size {} SOL", mode, mint[:8], symbol, base_sol, size_sol)
        mcap_sol = 0.0
        if FEED is not None:
            qm = FEED.stale_quote(mint, 600.0)
            if qm and qm.mcap_sol > 0:
                mcap_sol = qm.mcap_sol
        mcap_bit = f" • mcap ~{mcap_sol:.0f} SOL" if mcap_sol > 0 else ""
        _notify(f"🟢 {mode} OPEN {symbol} @{base_sol:.3e} SOL/tok • {size_sol} SOL{mcap_bit} • {mint[:6]}", sol=size_sol)
        return True
    except Exception as e:
        logger.warning("open_paper_position fail {}: {}", mint[:8], e)
        return False


async def _process_exit(pos_id: int, mint: str, price: float, sol_usd: float = 0.0):
    """Tiered scale-out (+40/60/80) with a ratcheting trailing stop. Each TP tier
    locks profit (sells 1/3); once in profit the stop ratchets up so we never give
    a winner back. Hard SL only applies before the first tier is hit.

    LIVE positions execute REAL sells via hunt.exec.live; proceeds come from the
    actual fill (parsed from the confirmed tx). If a live sell fails the position
    is KEPT open (never phantom-closed)."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        pos = conn.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
        if not pos or pos["status"] != "open":
            conn.close(); return
        entry = _entry_sol(pos)
        if entry <= 0:
            conn.close(); return
        mode = pos["mode"]
        tokens = pos["tokens"]
        size_sol = pos["size_sol"]
        tp_tier = int(pos["tp_tier"] or 0)
        realized = float(pos["realized_sol"] or 0.0)
        peak = max(_peak_sol(pos, entry), price)
        if price > _peak_sol(pos, entry):
            conn.execute("UPDATE positions SET peak_price_sol=? WHERE id=?", (price, pos_id))
            conn.commit()
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
                return units * price, units
            raw_bal = await ex._token_balance_raw(mint)
            if raw_bal <= 0:
                return 0.0, 0.0
            raw = min(max(1, int(units * 10 ** decimals)), raw_bal) if units > 0 else raw_bal
            r = await ex.sell(mint, raw, close_ata=(raw == raw_bal))
            if not r or not r.ok:
                _notify(f"🆘 SELL FAILED {pos['symbol']} (pos kept open) — tx error, retrying next tick")
                return None, 0.0
            # an exit that asked to sell the WHOLE balance but left coins behind
            # is a partial slice, not a close — keep the position open (the
            # proceeds may have been verified but the bag is NOT flat; closing
            # here would orphan the remainder / rep a fabricated full loss).
            if units == 0:
                rem = await ex._token_balance_raw(mint)
                if rem > 0:
                    _notify(f"🆘 PARTIAL SELL {pos['symbol']} ({units_sold:.4g}/{units_sold} SOL got, {rem} tokens left) — position kept open")
                    return None, 0.0
            _gap = int(getattr(r, "gap_bps", 0) or 0)
            if _gap > 0:
                from hunt.config import get_settings as _gs2
                if _gap > int(_gs2().sell_gap_guard_bps or 0):
                    _notify(f"⚠️ {mode} FILL GAP {pos['symbol']}: {_gap}bps short (got {r.sol_lamports/1e9:.4f} vs exp {r.expected/1e9:.4f} SOL)")
            units_sold = (-r.tokens_raw) / 10 ** decimals if r.tokens_raw < 0 else raw / 10 ** decimals
            # an exit that asked to sell the WHOLE balance but left coins behind
            # is a partial slice, not a close — keep the position open (the
            # proceeds may have been verified but the bag is NOT flat; closing
            # here would orphan the remainder / rep a fabricated full loss).
            if units == 0:
                rem = await ex._token_balance_raw(mint)
                if rem > 0:
                    _notify(f"🆘 PARTIAL SELL {pos['symbol']} ({r.sol_lamports/1e9:.4f} SOL got, {rem} tokens left) — position kept open")
                    return None, 0.0
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
            conn.execute("UPDATE positions SET tokens=?, tp_tier=?, realized_sol=?, peak_price_sol=? WHERE id=?",
                         (tokens, tp_tier, realized, peak, pos_id))
            conn.commit()
            logger.info("{} tp{} {} +{:.0f}% slice {:+.4f} SOL [ws]", mode, mint[:8], tp_tier, TIER_TRIGGERS[tp_tier - 1] * 100, slice_pnl)
            _notify(f"💰 {mode} TP{tp_tier} {pos['symbol']} +{TIER_TRIGGERS[tp_tier-1]*100:.0f}% slice {slice_pnl:+.4f} SOL", sol=slice_pnl)
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
                _notify(f"🔒 {mode} BREAKEVEN STOP {pos['symbol']} {realized:+.4f} SOL", sol=realized)
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
                _notify(f"🌙 {mode} MOON BAG CLOSED {pos['symbol']} {realized:+.4f} SOL (peak {peak_mult*100:.0f}% of entry, {trail_pct*100:.0f}% trail)", sol=realized)
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
            _notify(f"🛑 {mode} SL {pos['symbol']} {realized:+.4f} SOL ({change_pct:.0f}%)", sol=realized)
            return
        conn.close()
    except Exception as e:
        logger.debug("process exit error {}", e)


async def _handle_price_update(mint: str, price_sol: float, sol_usd: float = 0.0):
    if price_sol <= 0 or not mint: return
    if price_sol > 10:
        return
    _last_ws_ts[mint] = time.time()
    _ws_price[mint] = price_sol
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        pos = conn.execute("SELECT id FROM positions WHERE mint=? AND mode IN ('PAPER','LIVE') AND status='open' LIMIT 1", (mint,)).fetchone()
        conn.close()
        if not pos:
            return
        await _process_exit(pos["id"], mint, price_sol)
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
        entry = _entry_sol(pos)
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
            proceeds = tokens * price
        cost_rem = size_sol * ((2.0 / 3.0) ** tp_tier)
        realized += proceeds - cost_rem
        conn.execute("UPDATE positions SET status='closed', closed_ts=?, exit_reason=?, exit_sol=?, pnl_sol=? WHERE id=?",
                     (int(time.time()), reason, realized, realized, pos_id))
        conn.commit(); conn.close()
        logger.info("{} close {} {} forced {:+.4f} SOL [ws]", mode, mint[:8], reason, realized)
        _notify(f"⏹ {mode} FORCE CLOSE {pos['symbol']} {reason} {realized:+.4f} SOL", sol=realized)
    except Exception as e:
        logger.debug("force close error {}", e)


async def price_ws_loop(stop_event: asyncio.Event):
    """Helius accountSubscribe — SOL ticks. No FX poll."""
    global FEED
    global FEED
    from hunt.paper.execq import offer_tick
    if FEED is None:
        FEED = PriceFeed(on_tick=lambda mint, q: offer_tick(mint, q.price_sol))
    await FEED.run(stop_event)


async def _live_exit_price(client: httpx.AsyncClient, ds, mint: str, live: bool,
                           ex=None, sol_usd: float = 0.0) -> tuple[float, str]:
    """Exit price + source for one open position. Curve cache -> GeckoTerminal
    -> DexScreener -> Jupiter sell-quote (LIVE only). Paper path deliberately
    unchanged (old behavior exactly).

    Two structural rules that Apple (-98%) and eL5f (-27%) proved necessary:
    1) QUORUM: with >=2 disagreeing sources the stop evaluates against the
       LOWEST credible price (a stale-high source must never suppress a stop).
    2) Jupiter reads pool state directly, so it prices newborn pools the
       aggregators haven't indexed yet. If a route exists, the stop works."""
    cands: list[tuple[float, str]] = []
    base = await _price_for_mint_fallback(client, mint)
    if base > 0:
        cands.append((base, "fallback"))
    if live:
        try:
            dx, _liq = await ds.price_for_mint(mint)
            if dx > 0:
                cands.append((dx, "dex"))
        except Exception:
            pass
    if live and not cands and ex is not None and sol_usd > 0:
        try:
            ts, px = _jup_px_cache.get(mint, (0.0, 0.0))
            if time.time() - ts > 15.0:
                from hunt.exec.jupiter import JupiterClient
                from hunt.config import WSOL
                raw_bal = await ex._token_balance_raw(mint)
                px = 0.0
                if raw_bal > 0:
                    q = await JupiterClient(client).quote(mint, WSOL, raw_bal)
                    if q and q.out_amount_raw > 0:
                        dec = await ex._token_decimals(mint)
                        ui = raw_bal / (10 ** dec)
                        if ui > 0:
                            px = (q.out_amount_raw / 1e9) * sol_usd / ui
                _jup_px_cache[mint] = (time.time(), px)
            if px > 0:
                cands.append((px, "jup"))
        except Exception as e:
            logger.debug("jup exit quote failed {}: {}", mint[:8], e)
    if not cands:
        return 0.0, "none"
    if len(cands) >= 2:
        from hunt.config import get_settings as _gs3
        lo = min(p for p, _ in cands)
        hi = max(p for p, _ in cands)
        if lo > 0 and hi / lo - 1 > float(_gs3().live_price_quorum_pct or 15) / 100:
            logger.warning("exit price disagreement {}: {} — stops use lowest",
                           mint[:8], [(s, round(p, 8)) for p, s in cands])
    cands.sort()
    return cands[0][0], cands[0][1]


async def paper_stops_loop(stop_event: asyncio.Event):
    global _LIVE_HALTED
    # ws is the real-time pricing/exit engine for bonding-curve tokens.
    # This poll is a fallback for mints the ws hasn't ticked (graduated/Raydium),
    # using GeckoTerminal, every 3s.
    ws_task = asyncio.create_task(price_ws_loop(stop_event))
    client = httpx.AsyncClient(timeout=10)
    # Last-resort pricer for LIVE exits only (see uncovered loop below).
    ds_exits = DexScreener(client)
    _poll_s = float(get_settings().paper_stops_poll_s or 3.0)
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
                kill_path = os.path.abspath(get_settings().kill_file)
                if os.path.exists(kill_path):
                    logger.warning("KILL FILE DETECTED — force-closing all LIVE positions")
                    closed_n = 0
                    for pos in list(positions):
                        if pos["mode"] == "LIVE":
                            cur = 0.0
                            if FEED is not None:
                                q = FEED.stale_quote(pos["mint"], 600.0)
                                cur = q.price_sol if q else 0.0
                            if cur <= 0:
                                cur = _entry_sol(pos)
                            await _force_close(pos["id"], pos["mint"], cur, 0.0, "kill")
                            closed_n += 1
                    positions = [p for p in positions if p["mode"] != "LIVE"]
                    try:
                        os.remove(kill_path)
                    except Exception as e:
                        # e.g. root-owned file while running as hunt — the closes
                        # above already happened; stay fail-closed and say so.
                        logger.warning("kill file processed but cannot remove {}: {}", kill_path, e)
                    if closed_n > 0:
                        _notify(f"🛑 KILL FILE processed — {closed_n} LIVE position(s) closed. Restart to resume.")
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
                                cur = 0.0
                                if FEED is not None:
                                    q = FEED.stale_quote(pos["mint"], 600.0)
                                    cur = q.price_sol if q else 0.0
                                if cur <= 0:
                                    cur = _entry_sol(pos)
                                await _force_close(pos["id"], pos["mint"], cur, 0.0, "daily_loss_cap")
                        positions = [p for p in positions if p["mode"] != "LIVE"]
            except Exception as e:
                # NEVER debug-logged: a broken loss-cap guard must scream. (The
                # _LIVE_HALTED UnboundLocalError hid here at debug for days
                # because the running process predated the global-decl fix.)
                logger.warning("daily loss cap guard FAILED: {}", e)
            # keep feed subscriptions in sync with open positions (self-healing
            # after reconnects; unsubscribes closed positions automatically)
            if FEED is not None:
                open_mints = {p["mint"] for p in positions}
                for m in open_mints:
                    await FEED.ensure_priced(m)
                for m in await FEED.subscribed_mints():
                    if m not in open_mints:
                        await FEED.unsubscribe(m)
            # HTTP poll only if Helius still hasn't ticked this mint
            uncovered = [p for p in positions if now - _last_ws_ts.get(p["mint"], 0) > _poll_s]
            _blind_s = float(get_settings().live_blind_alert_s or 120)
            for pos in uncovered:
                price = 0.0
                if FEED is not None:
                    q = FEED.quote(pos["mint"], 30.0) or FEED.stale_quote(pos["mint"], 300.0)
                    if q:
                        price = q.price_sol
                if pos["mode"] == "LIVE":
                    if price > 0:
                        _px_seen[pos["mint"]] = (now, "helius")
                    else:
                        last, _src = _px_seen.get(pos["mint"], (0.0, ""))
                        if now - last > _blind_s and now - _blind_warned.get(pos["mint"], 0.0) > 600.0:
                            _blind_warned[pos["mint"]] = now
                            _notify(f"👁 BLIND {pos['symbol'] or pos['mint'][:8]} — no Helius tick for {now-last:.0f}s")
                            logger.warning("LIVE position blind {} for {:.0f}s", pos["mint"][:8], now - last)
                if price <= 0:
                    continue
                await _process_exit(pos["id"], pos["mint"], price)
            for pos in positions:
                hold_cap = 24*3600 if int(pos["tp_tier"] or 0) >= 2 else 6*3600
                if now - pos["opened_ts"] > hold_cap:
                    price = 0.0
                    if FEED is not None:
                        q = FEED.stale_quote(pos["mint"], 600.0)
                        price = q.price_sol if q else 0.0
                    if price <= 0:
                        price = _entry_sol(pos)
                    await _force_close(pos["id"], pos["mint"], price, 0.0, "max_hold")
        except Exception as e:
            logger.debug("paper stops poll error {}", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_poll_s)
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
    api_mc = 0.0
    try:
        api_mc = float(coin.get("market_cap") or 0)
    except (TypeError, ValueError):
        api_mc = 0.0
    got = _fdv_from_payload(coin)
    if got is None:
        got = await _onchain_mcap(client, mint)
    if got is None:
        coin["_mcap_ok"] = False
        logger.info("[MCAPRELI] {} {} api={:.0f} onchain=NO_CURVE/err", symbol, mint, api_mc)
    else:
        on_mc, on_grad = got
        if on_grad:
            coin["_graduated"] = True
            pool = coin.get("pool_address") or ""
            if not pool:
                try:
                    r = await client.get(f"{API}/{mint}", timeout=8)
                    if r.status_code == 200:
                        pool = (r.json() or {}).get("pool_address") or ""
                        coin["pool_address"] = pool
                except Exception:
                    pool = ""
            amm_mc = None
            if pool:
                try:
                    from hunt.paper.onchain_intel import fetch_amm_mcap
                    amm_mc = await fetch_amm_mcap(client, get_settings().rpc_http, pool)
                except Exception:
                    amm_mc = None
            if amm_mc and amm_mc > 0:
                coin["_mcap_ok"] = True
                coin["market_cap"] = amm_mc
                logger.info("[MCAPRELI] {} {} api={:.0f} AMM={:.1f} SOL", symbol, mint, api_mc, amm_mc)
            else:
                coin["_mcap_ok"] = False
                logger.info("[MCAPRELI] {} {} api={:.0f} GRADUATED no AMM mark", symbol, mint, api_mc)
        elif on_mc is None or on_mc <= 0:
            coin["_mcap_ok"] = False
            logger.info("[MCAPRELI] {} {} api={:.0f} onchain=drained", symbol, mint, api_mc)
        else:
            coin["_mcap_ok"] = True
            coin["market_cap"] = on_mc
            ratio = (api_mc / on_mc) if on_mc > 0 else 0.0
            logger.info("[MCAPRELI] {} {} api={:.0f} onchain={:.1f} SOL (api/onchain={:.1f}x)",
                        symbol, mint, api_mc, on_mc, ratio)
    mc_now = float(coin.get("market_cap") or 0)
    playable = coin.get("_mcap_ok") and mc_now >= 50.0 and (
        coin.get("_graduated") or mc_now <= MCAP_CEILING_SOL)
    if playable and not coin.get("_intel_ok"):
        intel = await _coin_intel(client, mint)
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
            conn = sqlite3.connect(DB_PATH, timeout=10)
            prior = conn.execute(
                """SELECT COUNT(DISTINCT pd.mint),
                          SUM(CASE WHEN po.pnl_sol > 0.5 THEN 1 ELSE 0 END),
                          SUM(CASE WHEN po.exit_reason='stop_loss' AND (po.closed_ts-po.opened_ts) < 900 THEN 1 ELSE 0 END)
                   FROM paper_decisions pd
                   JOIN positions po ON po.mint=pd.mint AND po.mode='PAPER' AND po.status='closed'
                   WHERE pd.dev=? AND pd.mint != ?""", (dev, mint)).fetchone()
            conn.close()
            n = int(prior[0] or 0) if prior else 0
            runners = int(prior[1] or 0) if prior else 0
            fast_rugs = int(prior[2] or 0) if prior else 0
            if n >= 2 and runners == 0 and fast_rugs >= 2:
                accept, reason = False, f"serial_rugger_{fast_rugs}"
            elif runners >= 1:
                reason = f"{reason}+dev_winner"
        except Exception:
            pass

    # smart wallet boost (automaton replication lineage: if tracked wallet bought, boost)
    smart_reason = ""
    try:
        from hunt.utils.smart_wallet import check_smart_buy
        is_smart, who = await check_smart_buy(mint)
        if is_smart and not accept:
            locked = reason.startswith((
                "intel_unavailable", "mcap_unavailable", "top10_heavy", "graduated",
                "snipers_", "rugged",
            ))
            if not locked:
                accept = True
                reason = f"smart_boost_{who}"
                smart_reason = who
        elif is_smart:
            reason = f"{reason}+smart_{who}"
    except: pass
    # SolanaTracker risk gate (zostaff style: risk>7 veto, top10≥80%, dev≥25%)
    tracker_reason = ""
    tracker_risk = None
    if accept:
        try:
            from hunt.utils.solanatracker import check_risk
            ok_risk, tracker_reason, tracker_risk = await check_risk(mint, client)
            if not ok_risk:
                accept = False
                reason = tracker_reason
            elif tracker_reason:
                reason = f"{reason}+{tracker_reason}"
        except Exception:
            accept = False
            reason = "snipers_unavailable"

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(SCHEMA)
    tj = json.dumps(tracker_risk) if tracker_risk else None
    conn.execute(
        f"INSERT OR IGNORE INTO {PAPER_DB_TABLE} (mint,symbol,created_ts,decision,reason,twitter,telegram,website,market_cap,decided_at,dev,burst,top10,snipers,holders,dev_pct,tracker_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mint, symbol, created_ts, "ACCEPT" if accept else "REJECT", reason,
         coin.get("twitter"), coin.get("telegram"), coin.get("website"),
         float(coin.get("market_cap") or 0), now, dev, coin.get("_burst"),
         coin.get("_top10"), None, coin.get("_holders"), coin.get("_dev_pct"), tj),
    )
    conn.commit()
    conn.close()
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now))} | {mint} | {symbol} | {'ACCEPT' if accept else 'REJECT'} | {reason}{'/'+tracker_reason if tracker_reason and tracker_reason!='risk_'+tracker_reason else ''} | twitter={bool(coin.get('twitter'))} website={bool(coin.get('website'))} hour={time.gmtime(created_ts).tm_hour if created_ts else '?'}UTC"
    if accept:
        stats["accepted"] += 1
        logger.info("ACCEPT {} {} ({}) [{}]", mint[:8], symbol, reason, tracker_reason)
        try:
            from hunt.paper.execq import offer_open
            offer_open(mint, symbol, ds)
        except Exception as e:
            logger.debug("open enqueue failed {}: {}", mint[:8], e)
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
        # ---- LIVE arm gate: starting the service is NOT consent to trade.
        # An operator must deliberately create hunt/data/live_armed first.
        arm = os.path.abspath(_s.live_arm_file)
        if not os.path.exists(arm):
            _notify("🔴 LIVE REFUSED — no live_armed marker (create hunt/data/live_armed to arm live trading)")
            logger.critical("LIVE refused: {} missing", arm)
            raise SystemExit("LIVE refused: not armed")
        _notify(f"🔥 LIVE TRADING STARTED — {ex.wallet[:8]}… {bal:.3f} SOL • {duration_s//60} min window")
    else:
        _notify(f"🤖 hunt paper run started — {duration_s//60} min window")
    start = time.time()
    end = start + duration_s
    global _RUN_END_TS
    _RUN_END_TS = end
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
    from hunt.paper.execq import init as execq_init, open_worker, tick_workers
    from hunt.paper.smart_seed import smart_seed_loop
    from hunt.watch.discovery_ws import new_tokens_loop
    execq_init()
    from hunt.paper.execq import offer_tick as _offer_tick
    global FEED
    if FEED is None:
        FEED = PriceFeed(on_tick=lambda mint, q: _offer_tick(mint, q.price_sol))
    stop_evt = asyncio.Event()
    tick_task = asyncio.create_task(tick_workers(stop_evt, 4))
    open_task = asyncio.create_task(open_worker(stop_evt, stats))
    smart_task = asyncio.create_task(smart_seed_loop(stop_evt))
    stops_task = asyncio.create_task(paper_stops_loop(stop_evt))
    hb_task = asyncio.create_task(heartbeat_loop(stop_evt, 60))
    disc_task = asyncio.create_task(new_tokens_loop(stop_evt, on_new_token))
    from hunt.notify.paper_control import run_paper_control
    ctrl_task = asyncio.create_task(run_paper_control(stop_evt))
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
    for t in (hb_task, disc_task, stops_task, ctrl_task, tick_task, open_task, smart_task):
        try: t.cancel()
        except: pass
    try:
        await stops_task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        # NEVER swallow: a dead stops loop = no exits, no kill-file, no cap.
        # (An UnboundLocalError here once silenced the whole loop for a session.)
        logger.error("stops loop died: {}", e)
    # final stats
    try:
        conn = sqlite3.connect(DB_PATH)
        open_n = conn.execute("SELECT COUNT(*) FROM positions WHERE mode IN ('PAPER','LIVE') AND status='open'").fetchone()[0]
        closed = conn.execute("SELECT COUNT(*), COALESCE(SUM(pnl_sol),0) FROM positions WHERE mode IN ('PAPER','LIVE') AND status='closed' AND opened_ts>=?", (int(start),)).fetchone()
        try:
            live_open = conn.execute("SELECT COUNT(*) FROM positions WHERE mode='LIVE' AND status='open'").fetchone()[0]
        except Exception:
            live_open = 0
        conn.close()
        if live_open:
            # bags outliving their manager is how Apple/eL5f bled unmanaged.
            _notify(f"⚠️ ENGINE STOPPING with {live_open} LIVE bag(s) UNMANAGED — restart to manage or close manually")
            logger.warning("stopping with {} LIVE positions open", live_open)
            await asyncio.sleep(2.0)  # let the scream flush before exit
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
    from hunt.utils.pidlock import acquire_lock, release_lock
    if not acquire_lock():
        print("another hunt instance is running (state/hunt.lock) — exiting")
        raise SystemExit(1)
    import signal as _sig

    def _release(signum, _frame):
        try:
            release_lock()
        except Exception:
            pass
        raise SystemExit(128 + signum)

    _sig.signal(_sig.SIGTERM, _release)
    try:
        asyncio.run(run_paper(duration_s=dur))
    finally:
        release_lock()
