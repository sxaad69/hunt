from __future__ import annotations

import asyncio
import signal
import sys

from loguru import logger

from hunt.backtest.pipeline import run_pipeline
from hunt.config import get_settings
from hunt.db.database import Database
from hunt.exec.executor import TradeExecutor
from hunt.notify.base import AppState, Notifier
from hunt.notify.control import run_control_bot
from hunt.scout.scout import run_scout
from hunt.score.scorer import run_scorer
from hunt.select.selector import Selector, run_selector
from hunt.universe.builder import run_universe
from hunt.watch.watcher import Watcher, run_watcher


def validate_startup(s) -> list[str]:
    problems = []
    if not s.helius_api_key:
        problems.append("HUNT_HELIUS_API_KEY missing — wallet streaming & replay disabled")
    if not s.birdeye_api_key:
        problems.append("HUNT_BIRDEYE_API_KEY missing — scout limited to pump.fun source")
    if not s.telegram_bot_token or not s.telegram_chat_id:
        problems.append("Telegram not configured — alerts/control disabled")
    if not s.dry_run and not s.wallet_private_key:
        problems.append("LIVE mode requires HUNT_WALLET_PRIVATE_KEY")
    return problems


async def main() -> None:
    s = get_settings()
    setup_logging_safe()

    from hunt.utils.pidlock import acquire_lock, release_lock
    if not acquire_lock():
        logger.error("another hunt instance is running (state/hunt.lock) — exiting")
        return

    for p in validate_startup(s):
        logger.warning("{}", p)

    mode = "PAPER" if s.dry_run else "LIVE"
    logger.info(
        "hunt starting | mode={} | size={} SOL | tracked_max={} | tp={} sl={} trail={}",
        mode, s.trade_size_sol, s.max_tracked_wallets,
        s.take_profit_pct, s.stop_loss_pct, s.trailing_stop_pct,
    )

    db = Database(f"{s.data_dir}/hunt.sqlite3")
    await db.connect()

    state = AppState()
    notifier = Notifier(s.telegram_bot_token, s.telegram_chat_id)
    notifier.start()

    executor = TradeExecutor(db, notifier)

    selector = Selector(db, notifier)
    watcher = Watcher(db, notifier, state, executor)

    from hunt.utils.health import health_server
    tasks = [
        asyncio.create_task(run_control_bot(db, state, notifier), name="telegram"),
        asyncio.create_task(run_scout(db, notifier), name="scout"),
        asyncio.create_task(run_scorer(db, notifier), name="scorer"),
        asyncio.create_task(run_selector(db, notifier), name="selector"),
        asyncio.create_task(watcher.run(), name="watcher"),
        asyncio.create_task(run_universe(db), name="universe"),
        asyncio.create_task(run_pipeline(db, notifier), name="pipeline"),
        asyncio.create_task(health_server(8080), name="health"),
    ]

    await notifier.send(
        f"🚀 hunt online [{mode}] — paper={'yes' if s.dry_run else 'NO'}\n"
        f"trade size {s.trade_size_sol} SOL · max {s.max_open_positions} positions"
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig_name, stop_event.set)
        except NotImplementedError:
            pass

    try:
        await stop_event.wait()
    finally:
        logger.info("shutting down…")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await notifier.send("🛑 hunt offline")
        await notifier.stop()
        await db.close()
        try:
            from hunt.utils.pidlock import release_lock
            release_lock()
        except: pass


def setup_logging_safe() -> None:
    from hunt.log import setup_logging

    setup_logging(get_settings().log_level)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
