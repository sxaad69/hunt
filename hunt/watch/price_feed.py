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
SOL_REFRESH_S = 45.0
# soft cap — free Helius plans cap active websocket subscriptions per key
MAX_SUBSCRIPTIONS = 40


def bonding_curve_pda(mint: str) -> str:
    return str(Pubkey.find_program_address(
        [b"bonding-curve", bytes(Pubkey.from_string(mint))], PUMP_PROGRAM)[0])


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
        self._sub_ids: dict[str, int] = {}     # curve pda -> ws subscription id
        self._pending: dict[str, str] = {}     # request id -> curve pda
        self._ws = None
        self._client = httpx.AsyncClient(timeout=15)

    # ---- public API -------------------------------------------------
    def quote(self, mint: str, max_age_s: float = 10.0) -> Quote | None:
        q = self._quotes.get(mint)
        if q and q.price_usd > 0 and q.ts > 0 and time.time() - q.ts <= max_age_s:
            return q
        return None

    def stale_quote(self, mint: str, max_age_s: float = 600.0) -> Quote | None:
        """Last known price within max_age_s — better than nothing for exits."""
        q = self._quotes.get(mint)
        if q and q.price_usd > 0 and q.ts > 0 and time.time() - q.ts <= max_age_s:
            return q
        return None

    def price_usd(self, mint: str) -> float:
        q = self.stale_quote(mint)
        return q.price_usd if q else 0.0

    async def subscribe(self, mint: str):
        if mint in self._mints:
            return
        if len(self._mints) >= MAX_SUBSCRIPTIONS:
            logger.debug("[price-feed] subscription cap {} reached — skipping {}", MAX_SUBSCRIPTIONS, mint[:8])
            return
        try:
            pda = bonding_curve_pda(mint)
        except Exception:
            return  # not a valid solana mint (evm address etc.)
        self._mints[mint] = pda
        self._curve_to_mint[pda] = mint
        self._quotes.setdefault(mint, Quote(mint=mint))
        if self._ws is not None:
            await self._send_subscribe(pda)

    async def unsubscribe(self, mint: str):
        pda = self._mints.pop(mint, None)
        if not pda:
            return
        self._curve_to_mint.pop(pda, None)
        self._quotes.pop(mint, None)
        sid = self._sub_ids.pop(pda, None)
        if sid is not None and self._ws is not None:
            try:
                await self._ws.send(json.dumps({"jsonrpc": "2.0", "id": f"unsub-{pda[:8]}",
                                                "method": "accountUnsubscribe", "params": [sid]}))
            except Exception:
                pass

    async def subscribed_mints(self) -> list[str]:
        return list(self._mints.keys())

    async def refresh_sol_usd(self):
        try:
            from hunt.scout.dexscreener import DexScreener
            ds = DexScreener(self._client)
            px = await ds.price_for_mint("So11111111111111111111111111111111111111112")
            if px and px > 0:
                self.sol_usd = px
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
        sol_task = asyncio.create_task(self._sol_loop(stop_event))
        hb_task = asyncio.create_task(self._heartbeat(stop_event))
        backoff = 2.0
        while not stop_event.is_set():
            try:
                import websockets
                async with websockets.connect(self.url, open_timeout=20, ping_interval=None) as ws:
                    self._ws = ws
                    for pda in list(self._curve_to_mint):
                        await self._send_subscribe(pda)
                    logger.info("[price-feed] connected, tracking {} curve(s)", len(self._sub_ids))
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
        sol_task.cancel()
        hb_task.cancel()

    # ---- internals ---------------------------------------------------
    async def _sol_loop(self, stop_event: asyncio.Event):
        while not stop_event.is_set():
            await self.refresh_sol_usd()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=SOL_REFRESH_S)
            except asyncio.TimeoutError:
                pass

    async def _heartbeat(self, stop_event: asyncio.Event):
        """Periodic visibility into feed health for monitoring."""
        last_info = 0.0
        hb_last = -1
        while not stop_event.is_set():
            await asyncio.sleep(60)
            line = f"[price-feed] hb: subs={len(self._sub_ids)} ticks={self.ticks_received} sol_usd={self.sol_usd:.2f}"
            if self.ticks_received != hb_last or time.time() - last_info > 300:
                logger.info(line)
                last_info = time.time()
            else:
                logger.debug(line)
            hb_last = self.ticks_received

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
        mint = self._curve_to_mint.get(pda) if pda else None
        if not mint:
            return
        v = result.get("value")
        if not v:
            return
        try:
            data = base64.b64decode(v["data"][0])
            if len(data) < 16:
                return
            vt, vs = struct.unpack_from("<QQ", data, 8)
        except Exception:
            return
        q = self._quotes.setdefault(mint, Quote(mint=mint))
        q.ts = time.time()
        self.ticks_received += 1
        if vt == 0 or vs == 0:
            # zeroed reserves = migrated/graduated; price now lives on the AMM
            q.graduated = True
        else:
            q.graduated = bool(data[48]) if len(data) > 48 else False
            q.price_sol = (vs / 1e9) / (vt / 1e6)
            q.mcap_sol = q.price_sol * 1e9
            if self.sol_usd > 0:
                q.price_usd = q.price_sol * self.sol_usd
        if self.on_tick is not None:
            try:
                await self.on_tick(mint, q)
            except Exception as e:
                logger.debug("[price-feed] on_tick error: {}", e)

    async def aclose(self):
        try:
            await self._client.aclose()
        except Exception:
            pass
