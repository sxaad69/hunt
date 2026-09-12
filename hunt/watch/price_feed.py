"""Real-time pump.fun bonding-curve pricing over the Helius websocket.

One shared connection, accountSubscribe on each tracked token's bonding-curve
PDA. Every swap mutates the curve's virtual reserves, so each notification is
an exact trade print: price_sol = (vs/1e9) / (vt/1e6). Validated against the
frontend market_cap (within ~1-6%, fetch-timing skew only).

Free Helius plan: transactionSubscribe is paywalled, accountSubscribe is not.
Helius sends the current account state immediately on subscribe, so a fresh
subscription doubles as a one-shot price fetch.
"""
from __future__ import annotations

import asyncio
import base64
import json
import struct
import time
from dataclasses import dataclass, field

import httpx
from loguru import logger
from solders.pubkey import Pubkey

from hunt.config import get_settings

PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

# silence longer than this means the connection is dead even without ws pings
# (dead curves trade zero times, so silence is NORMAL — only reconnect when a
# connection that should be receiving traffic stays silent far too long)
IDLE_RECONNECT_S = 240.0
# soft cap — free Helius plans cap active websocket subscriptions per key
MAX_SUBSCRIPTIONS = 40


def bonding_curve_pda(mint: str) -> str:
    return str(Pubkey.find_program_address(
        [b"bonding-curve", bytes(Pubkey.from_string(mint))], PUMP_PROGRAM)[0])


def parse_spl_amount(data: bytes) -> int | None:
    """SPL token account amount (u64 at offset 64)."""
    if len(data) < 72:
        return None
    return struct.unpack_from("<Q", data, 64)[0]


def amm_price_sol(base_raw: int, quote_raw: int, *, base_decimals: int = 6) -> tuple[float, float] | None:
    """PumpSwap vaults: quote is WSOL, base is the coin. Returns (price_sol, mcap_sol)."""
    if base_raw <= 0 or quote_raw <= 0:
        return None
    price_sol = (quote_raw / 1e9) / (base_raw / (10 ** base_decimals))
    return price_sol, price_sol * 1e9


def parse_curve_quote(data: bytes, decimals: int = 6) -> tuple[float, float, bool] | None:
    """Return (price_sol, mcap_sol, graduated) from a bonding-curve account blob.

    mcap_sol = (virtual_sol/1e9) * (supply/virtual_token) — decimals cancel.
    price_sol uses the mint's decimals (not hardcoded 6).
    """
    if len(data) < 49:
        return None
    vt = struct.unpack_from("<Q", data, 8)[0]
    vs = struct.unpack_from("<Q", data, 16)[0]
    supply = struct.unpack_from("<Q", data, 40)[0]
    complete = data[48] != 0
    if complete or vt == 0 or vs == 0:
        return 0.0, 0.0, True
    dec = decimals if decimals and decimals > 0 else 6
    price_sol = (vs / 1e9) / (vt / (10 ** dec))
    mcap_sol = (vs / 1e9) * (supply / vt)
    return price_sol, mcap_sol, False


@dataclass
class Quote:
    mint: str
    price_sol: float = 0.0
    price_usd: float = 0.0
    mcap_sol: float = 0.0
    ts: float = 0.0
    graduated: bool = False


class PriceFeed:
    """Track bonding-curve prices for a dynamic set of mints over one ws."""

    def __init__(self, url: str = "", on_tick=None):
        key = get_settings().helius_api_key
        if not url:
            if not key:
                raise RuntimeError("HUNT_HELIUS_API_KEY empty — PriceFeed needs it")
            url = f"wss://mainnet.helius-rpc.com/?api-key={key}"
        self.url = url
        self.on_tick = on_tick  # async fn(mint, Quote) or None
        self.sol_usd = 0.0
        self.ticks_received = 0
        self._quotes: dict[str, Quote] = {}
        self._mints: dict[str, str] = {}       # mint -> curve pda
        self._curve_to_mint: dict[str, str] = {}
        self._sub_ids: dict[str, int] = {}     # account -> ws subscription id
        self._pending: dict[str, str] = {}     # request id -> account
        self._amm_vaults: dict[str, str] = {}  # vault ata -> mint
        self._amm_state: dict[str, dict] = {}  # mint -> vault amounts
        self._amm_locks: dict[str, asyncio.Lock] = {}
        self._decimals: dict[str, int] = {}
        self._ws = None
        self._client = httpx.AsyncClient(timeout=15)

    # ---- public API -------------------------------------------------
    def quote(self, mint: str, max_age_s: float = 10.0) -> Quote | None:
        q = self._quotes.get(mint)
        if q and q.price_sol > 0 and q.ts > 0 and time.time() - q.ts <= max_age_s:
            return q
        return None

    def stale_quote(self, mint: str, max_age_s: float = 600.0) -> Quote | None:
        q = self._quotes.get(mint)
        if q and q.price_sol > 0 and q.ts > 0 and time.time() - q.ts <= max_age_s:
            return q
        return None

    def price_usd(self, mint: str) -> float:
        q = self.stale_quote(mint)
        return q.price_usd if q else 0.0

    def _ws_acct_count(self) -> int:
        return len(self._sub_ids) + len(self._pending)

    def _room_for(self, n: int) -> bool:
        return self._ws_acct_count() + n <= MAX_SUBSCRIPTIONS

    async def ensure_priced(self, mint: str, pool_address: str | None = None) -> Quote | None:
        """WS-first mark: curve PDA if live; vaults only if graduated."""
        await self.subscribe(mint)
        q = self._quotes.get(mint)
        if (not q or q.price_sol <= 0) and mint in self._mints:
            await self._seed_quote(mint, self._mints[mint])
            q = self._quotes.get(mint)
        if q and q.graduated:
            await self._promote_amm(mint, pool_address)
            q = self._quotes.get(mint)
        if q and q.price_sol > 0 and q.ts > 0:
            return q
        return self.stale_quote(mint, 300.0)

    async def subscribe(self, mint: str):
        if mint in self._amm_state or mint in self._mints:
            return
        try:
            pda = bonding_curve_pda(mint)
        except Exception:
            return
        self._quotes.setdefault(mint, Quote(mint=mint))
        await self._mint_decimals(mint)
        await self._seed_quote(mint, pda)
        q = self._quotes.get(mint)
        if q and q.graduated:
            await self._promote_amm(mint)
            return
        if not self._room_for(1):
            logger.debug("[price-feed] subscription cap {} reached — skipping {}", MAX_SUBSCRIPTIONS, mint[:8])
            return
        self._mints[mint] = pda
        self._curve_to_mint[pda] = mint
        if self._ws is not None:
            await self._send_subscribe(pda)

    async def unsubscribe(self, mint: str):
        await self._drop_curve(mint)
        self._quotes.pop(mint, None)
        st = self._amm_state.pop(mint, None)
        if st:
            for key in ("base_vault", "quote_vault"):
                vault = st.get(key)
                if vault:
                    self._amm_vaults.pop(vault, None)
                    await self._unsub_account(vault)

    async def _drop_curve(self, mint: str):
        pda = self._mints.pop(mint, None)
        if not pda:
            return
        self._curve_to_mint.pop(pda, None)
        await self._unsub_account(pda)

    async def subscribed_mints(self) -> list[str]:
        return list(set(self._mints) | set(self._amm_state))

    async def refresh_sol_usd(self):
        try:
            from hunt.scout.dexscreener import DexScreener
            ds = DexScreener(self._client)
            px = await ds.price_for_mint("So11111111111111111111111111111111111111112")
            if isinstance(px, (tuple, list)):
                px = px[0] if px else 0
            if px and float(px) > 0:
                self.sol_usd = float(px)
                return
        except Exception:
            pass
        try:
            r = await self._client.get(
                "https://api.geckoterminal.com/api/v2/networks/solana/tokens/So11111111111111111111111111111111111111112",
                headers={"Accept-Encoding": "gzip"})
            if r.status_code == 200:
                pu = (r.json().get("data") or {}).get("attributes", {}).get("price_usd")
                if pu:
                    self.sol_usd = float(pu)
        except Exception:
            pass

    async def run(self, stop_event: asyncio.Event):
        """Main loop; reconnects forever until stop_event is set."""
        hb_task = asyncio.create_task(self._heartbeat(stop_event))
        backoff = 2.0
        while not stop_event.is_set():
            try:
                import websockets
                async with websockets.connect(self.url, open_timeout=20, ping_interval=None) as ws:
                    self._ws = ws
                    for pda in list(self._curve_to_mint):
                        await self._send_subscribe(pda)
                    for vault in list(self._amm_vaults):
                        await self._send_subscribe(vault)
                    logger.info("[price-feed] connected, tracking {} acct(s)", len(self._mints) + len(self._amm_vaults))
                    backoff = 2.0
                    last_msg = time.time()
                    while not stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            # silence only means a dead connection if we actually
                            # expect traffic; with zero subscriptions, idle is fine
                            if self._sub_ids and time.time() - last_msg > IDLE_RECONNECT_S:
                                logger.warning("[price-feed] idle {}s — reconnecting", IDLE_RECONNECT_S)
                                break
                            continue
                        last_msg = time.time()
                        await self._on_message(raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("[price-feed] down: {} — retry in {:.0f}s", e, backoff)
            finally:
                self._ws = None
                self._sub_ids.clear()
                self._pending.clear()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 1.7, 60.0)
        hb_task.cancel()

    # ---- internals ---------------------------------------------------
    async def _heartbeat(self, stop_event: asyncio.Event):
        """Periodic visibility into feed health for monitoring."""
        last_info = 0.0
        hb_last = -1
        while not stop_event.is_set():
            await asyncio.sleep(60)
            line = (f"[price-feed] hb: subs={len(self._sub_ids)} "
                    f"curve={len(self._mints)} amm={len(self._amm_state)} ticks={self.ticks_received}")
            if self.ticks_received != hb_last or time.time() - last_info > 300:
                logger.info(line)
                last_info = time.time()
            else:
                logger.debug(line)
            hb_last = self.ticks_received

    async def _mint_decimals(self, mint: str) -> int:
        if mint in self._decimals:
            return self._decimals[mint]
        try:
            r = await self._client.post(get_settings().rpc_http, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenSupply",
                "params": [mint],
            })
            d = (((r.json() or {}).get("result") or {}).get("value") or {}).get("decimals")
            self._decimals[mint] = int(d) if d is not None else 6
        except Exception:
            self._decimals[mint] = 6
        return self._decimals[mint]

    async def _seed_quote(self, mint: str, pda: str):
        try:
            r = await self._client.post(get_settings().rpc_http, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getAccountInfo",
                "params": [pda, {"encoding": "base64"}],
            })
            value = ((r.json() or {}).get("result") or {}).get("value")
            if not value:
                return
            raw = value.get("data") or []
            if not isinstance(raw, list) or not raw:
                return
            self._apply_curve_data(mint, base64.b64decode(raw[0]))
        except Exception:
            pass

    def _apply_curve_data(self, mint: str, data: bytes):
        parsed = parse_curve_quote(data, self._decimals.get(mint, 6))
        if parsed is None:
            return
        price_sol, mcap_sol, graduated = parsed
        q = self._quotes.setdefault(mint, Quote(mint=mint))
        q.ts = time.time()
        self.ticks_received += 1
        q.graduated = graduated
        if not graduated:
            q.price_sol = price_sol
            q.mcap_sol = mcap_sol
        elif mint not in self._amm_state:
            try:
                asyncio.get_running_loop().create_task(self._promote_amm(mint))
            except RuntimeError:
                pass

    async def _unsub_account(self, acct: str):
        sid = self._sub_ids.pop(acct, None)
        if sid is not None and self._ws is not None:
            try:
                await self._ws.send(json.dumps({"jsonrpc": "2.0", "id": f"unsub-{acct[:8]}",
                                                "method": "accountUnsubscribe", "params": [sid]}))
            except Exception:
                pass

    async def _promote_amm(self, mint: str, pool_address: str | None = None):
        lock = self._amm_locks.setdefault(mint, asyncio.Lock())
        async with lock:
            st0 = self._amm_state.get(mint)
            if st0 and st0.get("base_raw", 0) > 0 and st0.get("quote_raw", 0) > 0:
                self._apply_amm_quote(mint)
                await self._drop_curve(mint)
                return
            await self._promote_amm_locked(mint, pool_address)

    async def _promote_amm_locked(self, mint: str, pool_address: str | None = None):
        if not pool_address:
            try:
                r = await self._client.get(
                    f"https://frontend-api-v3.pump.fun/coins/{mint}", timeout=8)
                if r.status_code == 200:
                    pool_address = (r.json() or {}).get("pool_address") or ""
            except Exception:
                pool_address = None
        if not pool_address:
            logger.debug("[price-feed] no pool_address for graduated {}", mint[:8])
            return
        try:
            from hunt.exec.pumpfun.pumpswap import fetch_pool_state
            st = await fetch_pool_state(get_settings().rpc_http, pool_address, http_client=self._client)
        except Exception as e:
            logger.debug("[price-feed] pool state {} {}: {}", mint[:8], pool_address[:8], e)
            return
        if st.base_is_sol:
            logger.debug("[price-feed] skip inverted pool {}", mint[:8])
            return
        base_vault = str(st.pool_base_token_account)
        quote_vault = str(st.pool_quote_token_account)
        extra = 0
        if base_vault not in self._sub_ids and base_vault not in self._pending:
            extra += 1
        if quote_vault not in self._sub_ids and quote_vault not in self._pending:
            extra += 1
        freed = 1 if mint in self._mints else 0
        if extra - freed > 0 and not self._room_for(extra - freed):
            logger.debug("[price-feed] no room for AMM vaults {}", mint[:8])
            return
        self._amm_state[mint] = {
            "base_vault": base_vault, "quote_vault": quote_vault,
            "base_raw": 0, "quote_raw": 0,
        }
        self._amm_vaults[base_vault] = mint
        self._amm_vaults[quote_vault] = mint
        await self._seed_vault(mint, "base_raw", base_vault)
        await self._seed_vault(mint, "quote_raw", quote_vault)
        if self._ws is not None:
            await self._send_subscribe(base_vault)
            await self._send_subscribe(quote_vault)
        await self._drop_curve(mint)
        self._apply_amm_quote(mint)
        logger.info("[price-feed] AMM ws {} pool={} (curve unsubbed)", mint[:8], pool_address[:8])

    async def _seed_vault(self, mint: str, field: str, vault: str):
        try:
            r = await self._client.post(get_settings().rpc_http, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenAccountBalance",
                "params": [vault],
            })
            amt = ((r.json() or {}).get("result") or {}).get("value") or {}
            raw = amt.get("amount")
            if raw is not None:
                self._amm_state[mint][field] = int(raw)
                dec = amt.get("decimals")
                if dec is not None and field == "base_raw":
                    self._decimals[mint] = int(dec)
                return
        except Exception:
            pass
        try:
            r = await self._client.post(get_settings().rpc_http, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getAccountInfo",
                "params": [vault, {"encoding": "base64"}],
            })
            value = ((r.json() or {}).get("result") or {}).get("value")
            raw = (value or {}).get("data") or []
            if not isinstance(raw, list) or not raw:
                return
            amt = parse_spl_amount(base64.b64decode(raw[0]))
            if amt is not None:
                self._amm_state[mint][field] = amt
        except Exception:
            pass

    def _apply_amm_quote(self, mint: str):
        st = self._amm_state.get(mint)
        if not st:
            return
        parsed = amm_price_sol(st["base_raw"], st["quote_raw"],
                                base_decimals=self._decimals.get(mint, 6))
        if parsed is None:
            return
        price_sol, mcap_sol = parsed
        q = self._quotes.setdefault(mint, Quote(mint=mint))
        q.ts = time.time()
        q.graduated = True
        q.price_sol = price_sol
        q.mcap_sol = mcap_sol
        self.ticks_received += 1

    async def _send_subscribe(self, pda: str):
        assert self._ws is not None
        rid = f"sub-{len(self._pending)}-{pda[:8]}"
        self._pending[rid] = pda
        await self._ws.send(json.dumps({
            "jsonrpc": "2.0", "id": rid, "method": "accountSubscribe",
            "params": [pda, {"encoding": "base64", "commitment": "processed"}]}))

    async def _on_message(self, raw: str | bytes):
        try:
            d = json.loads(raw)
        except Exception:
            return
        if d.get("error"):
            logger.warning("[price-feed] rpc error: {}", d["error"])
            return
        rid = d.get("id")
        if rid is not None:
            rid = str(rid)
            pda = self._pending.pop(rid, None)
            if pda is not None and d.get("result") is not None:
                # u64 subscription id — keep as int (accountUnsubscribe requires it)
                try:
                    self._sub_ids[pda] = int(d["result"])
                except (TypeError, ValueError):
                    self._sub_ids[pda] = d["result"]
            return
        params = d.get("params") or {}
        result = params.get("result") or {}
        try:
            sid = int(params.get("subscription"))
        except (TypeError, ValueError):
            return
        pda = next((p for p, s in self._sub_ids.items() if s == sid), None)
        if not pda:
            return
        v = result.get("value")
        if not v:
            return
        try:
            data = base64.b64decode(v["data"][0])
        except Exception:
            return
        mint = self._curve_to_mint.get(pda)
        if mint:
            self._apply_curve_data(mint, data)
        else:
            mint = self._amm_vaults.get(pda)
            if not mint or mint not in self._amm_state:
                return
            amt = parse_spl_amount(data)
            if amt is None:
                return
            st = self._amm_state[mint]
            if pda == st["base_vault"]:
                st["base_raw"] = amt
            elif pda == st["quote_vault"]:
                st["quote_raw"] = amt
            else:
                return
            self._apply_amm_quote(mint)
        q = self._quotes.get(mint)
        if q is not None and self.on_tick is not None:
            try:
                maybe = self.on_tick(mint, q)
                if asyncio.iscoroutine(maybe):
                    asyncio.create_task(maybe)
            except Exception as e:
                logger.debug("[price-feed] on_tick error: {}", e)

    async def aclose(self):
        try:
            await self._client.aclose()
        except Exception:
            pass
