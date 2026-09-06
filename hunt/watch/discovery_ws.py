"""Event-driven pump.fun discovery via PumpPortal's free new-token websocket.

subscribeNewToken is free (no API key) and pushes every pump.fun launch within
~1s of creation — replaces the 30s /coins polling as the primary discovery
path. Note: trade subscriptions (subscribeTokenTrade) require a 0.02 SOL
funded API key, which is why pricing uses the Helius curve feed instead.
"""
from __future__ import annotations

import asyncio
import json
import time

from loguru import logger

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"


async def new_tokens_loop(stop_event: asyncio.Event, on_new_token, idle_reconnect_s: float = 180.0):
    """Call ``on_new_token(evt)`` for every pump.fun launch until stopped.
    Reconnects with backoff; also reconnects if no launch arrives in
    idle_reconnect_s (launches are constant, so silence = dead connection)."""
    backoff = 2.0
    while not stop_event.is_set():
        try:
            import websockets
            async with websockets.connect(PUMPPORTAL_WS, open_timeout=20, ping_interval=None) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                logger.info("[discovery] pumpportal new-token stream connected")
                backoff = 2.0
                last_msg = time.time()
                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except asyncio.TimeoutError:
                        if time.time() - last_msg > idle_reconnect_s:
                            logger.warning("[discovery] no launches in {}s — reconnecting", idle_reconnect_s)
                            break
                        continue
                    last_msg = time.time()
                    try:
                        evt = json.loads(raw)
                    except Exception:
                        continue
                    if not evt.get("mint"):
                        continue
                    try:
                        await on_new_token(evt)
                    except Exception as e:
                        logger.debug("[discovery] handler error: {}", e)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("[discovery] stream down: {} — retry in {:.0f}s", e, backoff)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 1.7, 60.0)
