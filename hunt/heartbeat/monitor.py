from __future__ import annotations
import asyncio, time
from loguru import logger
from hunt.survival.tiers import heartbeat_once

async def heartbeat_loop(stop_event: asyncio.Event, interval_s: int = 60):
    while not stop_event.is_set():
        try:
            data = heartbeat_once()
            logger.info("heartbeat tier={} pnl={:+.4f} SOL poll={} max_open={}", data["tier"], data["pnl_sol"], data["tier_cfg"]["poll_interval_s"], data["tier_cfg"]["max_open"])
            # self-mod: if critical, log warning like automaton low_compute
            if data["tier"] == "critical":
                logger.debug("CRITICAL tier — size={} max_open={}",
                             data["tier_cfg"]["trade_size_sol"], data["tier_cfg"]["max_open"])
            if data["tier"] == "dead":
                logger.error("DEAD tier — halting buys")
        except Exception as e:
            logger.debug("heartbeat error {}", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
