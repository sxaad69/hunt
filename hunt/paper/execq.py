"""Plan C: Helius recv never awaits buy/sell. Ticks and opens are signals."""
from __future__ import annotations

import asyncio

from loguru import logger

_tick_q: asyncio.Queue | None = None
_open_q: asyncio.Queue | None = None
_locks: dict[str, asyncio.Lock] = {}
_sem: asyncio.Semaphore | None = None


def init() -> None:
    global _tick_q, _open_q, _sem
    if _tick_q is None:
        _tick_q = asyncio.Queue(maxsize=4000)
        _open_q = asyncio.Queue(maxsize=200)
        _sem = asyncio.Semaphore(8)


def offer_tick(mint: str, price_sol: float) -> None:
    if not mint or price_sol <= 0 or _tick_q is None:
        return
    try:
        _tick_q.put_nowait((mint, float(price_sol)))
    except asyncio.QueueFull:
        try:
            _tick_q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            _tick_q.put_nowait((mint, float(price_sol)))
        except asyncio.QueueFull:
            pass


def offer_open(mint: str, symbol: str, ds) -> None:
    if not mint or _open_q is None:
        return
    try:
        _open_q.put_nowait((mint, symbol, ds))
    except asyncio.QueueFull:
        logger.warning("open queue full — dropped {}", mint[:8])


async def tick_workers(stop_event: asyncio.Event, n: int = 4) -> None:
    from hunt.paper.run import _handle_price_update

    async def worker():
        while not stop_event.is_set():
            try:
                mint, px = await asyncio.wait_for(_tick_q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except Exception:
                continue
            lock = _locks.setdefault(mint, asyncio.Lock())
            async with _sem:
                async with lock:
                    try:
                        await _handle_price_update(mint, px)
                    except Exception as e:
                        logger.debug("exec tick {}: {}", mint[:8], e)

    tasks = [asyncio.create_task(worker()) for _ in range(n)]
    try:
        await stop_event.wait()
    finally:
        for t in tasks:
            t.cancel()


async def open_worker(stop_event: asyncio.Event, stats: dict) -> None:
    from hunt.paper.run import open_paper_position

    while not stop_event.is_set():
        try:
            mint, symbol, ds = await asyncio.wait_for(_open_q.get(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        except Exception:
            continue
        try:
            ok = await open_paper_position(mint, symbol, ds)
            if ok:
                stats["opened"] += 1
        except Exception as e:
            logger.debug("exec open {}: {}", mint[:8], e)
