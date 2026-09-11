"""Telegram control for the paper/live engine (hunt.paper.run path).

The legacy control bot (hunt.notify.control) is wired to the old
scout/scorer/watcher app and shares nothing with the running engine — so the
deployed service had NO operator control. This module closes that hole with a
small command set against the engine's own SQLite + marker files:

  /status     engine mode/tier, open counts, today's realized PnL, pause/arm state
  /positions  open positions (mode/symbol/size/entry/peak)
  /pnl        realized PnL totals per mode
  /pause      stop ALL new opens (creates the pause file; survives restarts)
  /resume     clear pause
  /close_all  force-close every open LIVE position at current price (real sells)
  /kill       engage kill_live (force-close live next tick) + pause new opens
  /help       this message

Auth = the configured HUNT_TELEGRAM_CHAT_ID only; everyone else is ignored.
Stale updates are dropped on startup so a days-old /pause can't fire late.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time

from loguru import logger

HELP_TEXT = (
    "hunt engine control\n"
    "/status - mode/tier/opens/today PnL/pause+arm\n"
    "/positions - open positions\n"
    "/pnl - realized totals per mode\n"
    "/pause - stop new opens\n"
    "/resume - clear pause\n"
    "/close_all - force-close ALL live (real sells)\n"
    "/kill - kill_live + pause\n"
    "/help - this message"
)


def _db_rows(query: str, params: tuple = ()) -> list:
    from hunt.paper.run import DB_PATH
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(query, params).fetchall())
    finally:
        conn.close()


def _status_text() -> str:
    from hunt.config import get_settings
    s = get_settings()
    mode = "LIVE" if not s.dry_run else "PAPER"
    try:
        from hunt.survival.tiers import get_tier, current_pnl_sol
        tier = get_tier()
        tier_txt = f"{tier.name} (size {tier.trade_size_sol} max {tier.max_open})"
        pnl = current_pnl_sol()
    except Exception:
        tier_txt, pnl = "?", 0.0
    day_start = int(time.time()) - (int(time.time()) % 86400)
    lines = [f"mode={mode} tier={tier_txt} pnl_all={pnl:+.4f}"]
    for m in ("PAPER", "LIVE"):
        rows = _db_rows(
            "SELECT COUNT(*) n, COALESCE(SUM(CASE WHEN status='open' THEN 1 ELSE 0 END),0) o,"
            " COALESCE(SUM(CASE WHEN status='closed' AND closed_ts>=? THEN pnl_sol ELSE 0 END),0) d"
            " FROM positions WHERE mode=?", (day_start, m))
        r = rows[0] if rows else (0, 0, 0.0)
        lines.append(f"{m}: open={r[1]} today={r[2]:+.4f}")
    paused = os.path.exists(os.path.abspath(s.pause_file))
    armed = os.path.exists(os.path.abspath(s.live_arm_file))
    killed = os.path.exists(os.path.abspath(s.kill_file))
    lines.append(f"paused={paused} armed={armed} kill={killed}")
    return "\n".join(lines)


def _positions_text() -> str:
    rows = _db_rows(
        "SELECT id, mode, symbol, size_sol, entry_price_sol, peak_price_sol, tp_tier"
        " FROM positions WHERE status='open' ORDER BY opened_ts DESC LIMIT 30")
    if not rows:
        return "no open positions"
    out = []
    for r in rows:
        out.append(f"#{r['id']} {r['mode']} {r['symbol']} size={r['size_sol']:.4f}"
                    f" entry={r['entry_price_sol'] or 0:.3e} peak={r['peak_price_sol'] or 0:.3e} SOL")
    return "\n".join(out)


def _pnl_text() -> str:
    out = []
    for m in ("PAPER", "LIVE"):
        rows = _db_rows(
            "SELECT COUNT(*) n, COALESCE(SUM(pnl_sol),0) p FROM positions"
            " WHERE mode=? AND status='closed'", (m,))
        r = rows[0] if rows else (0, 0.0)
        out.append(f"{m}: closed={r[0]} realized={r[1]:+.4f} SOL")
    return "\n".join(out)


async def run_paper_control(stop_event: asyncio.Event) -> None:
    from hunt.config import get_settings
    s = get_settings()
    if not s.telegram_bot_token or not s.telegram_chat_id:
        logger.info("telegram control disabled (no creds)")
        return
    try:
        from aiogram import Bot, Dispatcher
        from aiogram.filters import Command
    except Exception as e:
        logger.warning("telegram control unavailable (aiogram missing): {}", e)
        return
    chat_id = s.telegram_chat_id
    bot = Bot(s.telegram_bot_token)
    dp = Dispatcher()

    def auth(m) -> bool:
        try:
            return m.chat.id == chat_id
        except Exception:
            return False

    @dp.message(Command("start", "help"))
    async def _help(m):
        if not auth(m):
            return
        await m.answer(HELP_TEXT)

    @dp.message(Command("status"))
    async def _status(m):
        if not auth(m):
            return
        try:
            await m.answer(_status_text())
        except Exception as e:
            await m.answer(f"status failed: {e}")

    @dp.message(Command("positions"))
    async def _positions(m):
        if not auth(m):
            return
        try:
            await m.answer(_positions_text())
        except Exception as e:
            await m.answer(f"positions failed: {e}")

    @dp.message(Command("pnl"))
    async def _pnl(m):
        if not auth(m):
            return
        try:
            await m.answer(_pnl_text())
        except Exception as e:
            await m.answer(f"pnl failed: {e}")

    @dp.message(Command("pause"))
    async def _pause(m):
        if not auth(m):
            return
        try:
            open(os.path.abspath(s.pause_file), "a").close()
            logger.warning("paused via telegram")
            await m.answer("paused — no new opens until /resume")
        except Exception as e:
            await m.answer(f"pause failed: {e}")

    @dp.message(Command("resume"))
    async def _resume(m):
        if not auth(m):
            return
        try:
            os.remove(os.path.abspath(s.pause_file))
        except Exception:
            pass
        logger.warning("resumed via telegram")
        await m.answer("resumed")

    @dp.message(Command("close_all"))
    async def _close_all(m):
        if not auth(m):
            return
        try:
            from hunt.paper.run import FEED, _force_close
            rows = _db_rows("SELECT id, mint FROM positions WHERE mode='LIVE' AND status='open'")
            if not rows:
                await m.answer("no open LIVE positions")
                return
            n, failed = 0, 0
            for r in rows:
                px = 0.0
                if FEED is not None:
                    q = FEED.stale_quote(r["mint"], 600.0)
                    px = q.price_sol if q else 0.0
                if px <= 0:
                    failed += 1
                    continue
                await _force_close(r["id"], r["mint"], px, 0.0, "telegram_close_all")
                n += 1
            logger.warning("telegram /close_all: closed={} priceless={}", n, failed)
            await m.answer(f"close_all done: closed={n} no_price={failed}")
        except Exception as e:
            logger.error("telegram /close_all failed: {}", e)
            await m.answer(f"close_all failed: {e}")

    @dp.message(Command("kill"))
    async def _kill(m):
        if not auth(m):
            return
        try:
            open(os.path.abspath(s.kill_file), "a").close()
            open(os.path.abspath(s.pause_file), "a").close()
            logger.warning("KILL engaged via telegram (kill_live + pause)")
            await m.answer("KILL engaged — live closes next tick, new opens paused")
        except Exception as e:
            await m.answer(f"kill failed: {e}")

    try:
        # drop stale updates first: a days-old /pause must never fire late.
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass
        logger.info("telegram control live for chat {}", chat_id)
        await dp.start_polling(bot, handle_signals=False)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("telegram control crashed: {}", e)
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass
