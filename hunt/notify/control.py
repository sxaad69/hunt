from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from loguru import logger

from hunt.config import get_settings
from hunt.notify.base import fmt_age

if TYPE_CHECKING:
    from hunt.db.database import Database
    from hunt.notify.base import AppState, Notifier

HELP_TEXT = (
    "<b>hunt bot</b>\n"
    "/status - engine state + today's PnL\n"
    "/positions - open positions\n"
    "/pnl - all-time stats\n"
    "/wallets - tracked wallets + scores\n"
    "/pause - pause new buys\n"
    "/resume - resume (clears pause)\n"
    "/kill - KILL SWITCH: halt all buys\n"
    "/help - this message"
)


class ControlBot:
    def __init__(self, db: Database, state: AppState, notifier: Notifier) -> None:
        s = get_settings()
        self.db = db
        self.state = state
        self.notifier = notifier
        self.bot = Bot(s.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = Dispatcher()
        self.chat_id = s.telegram_chat_id
        self._register()

    def _authorized(self, chat_id: int) -> bool:
        return chat_id == self.chat_id

    def _register(self) -> None:
        dp = self.dp

        @dp.message(CommandStart())
        async def start(m):
            if not self._authorized(m.chat.id):
                return
            await m.answer(HELP_TEXT)

        @dp.message(Command("help"))
        async def help_cmd(m):
            if not self._authorized(m.chat.id):
                return
            await m.answer(HELP_TEXT)

        @dp.message(Command("status"))
        async def status(m):
            if not self._authorized(m.chat.id):
                return
            mode = "PAPER" if get_settings().dry_run else "LIVE"
            tracked = await self.db.count_wallets_by_status("tracked")
            candidates = await self.db.count_wallets_by_status("candidate")
            stats = await self.db.total_stats(mode)
            flags = []
            if self.state.kill_switch:
                flags.append("KILL SWITCH ON")
            if self.state.paused:
                flags.append("PAUSED")
            txt = (
                f"<b>status</b> [{mode}]\n"
                f"tracked wallets: {tracked}\n"
                f"candidate pool: {candidates}\n"
                f"open positions: {stats['open_positions']} ({stats['open_sol']} SOL)\n"
                f"closed trades: {stats['closed_trades']}\n"
                f"realized pnl: {stats['realized_pnl_sol']} SOL"
            )
            if flags:
                txt += "\n⚠️ " + ", ".join(flags)
            await m.answer(txt)

        @dp.message(Command("positions"))
        async def positions(m):
            if not self._authorized(m.chat.id):
                return
            mode = "PAPER" if get_settings().dry_run else "LIVE"
            rows = await self.db.get_open_positions(mode)
            if not rows:
                await m.answer("no open positions")
                return
            lines = []
            for r in rows[:15]:
                lines.append(
                    f"#{r['id']} {r['symbol'] or '?'} | {r['size_sol']:.3f} SOL "
                    f"| entry ${r['entry_price_usd'] or 0:.6g} | {fmt_age(r['opened_ts'])}"
                )
            await m.answer("\n".join(lines))

        @dp.message(Command("pnl"))
        async def pnl(m):
            if not self._authorized(m.chat.id):
                return
            mode = "PAPER" if get_settings().dry_run else "LIVE"
            stats = await self.db.total_stats(mode)
            daily = await self.db.daily_realized_pnl_sol(mode)
            await m.answer(
                f"<b>pnl</b> [{mode}]\n"
                f"today: {daily:+.4f} SOL\n"
                f"all-time: {stats['realized_pnl_sol']:+.4f} SOL over {stats['closed_trades']} closed trades"
            )

        @dp.message(Command("wallets"))
        async def wallets(m):
            if not self._authorized(m.chat.id):
                return
            rows = await self.db.get_wallets_by_status("tracked")
            if not rows:
                await m.answer("no tracked wallets yet")
                return
            lines = []
            for r in rows:
                score = f"{r['score']:.2f}" if r["score"] is not None else "-"
                short = r["address"][:6] + "…" + r["address"][-4:]
                lines.append(f"{score} | {short} | src {r['source']}")
            await m.answer("<b>tracked</b>\n" + "\n".join(lines))

        @dp.message(Command("pause"))
        async def pause(m):
            if not self._authorized(m.chat.id):
                return
            self.state.paused = True
            await m.answer("⏸ paused: no new buys")

        @dp.message(Command("resume"))
        async def resume(m):
            if not self._authorized(m.chat.id):
                return
            self.state.paused = False
            self.state.kill_switch = False
            await m.answer("▶️ resumed")

        @dp.message(Command("kill"))
        async def kill(m):
            if not self._authorized(m.chat.id):
                return
            self.state.kill_switch = True
            logger.warning("KILL SWITCH engaged via telegram")
            await m.answer("🛑 KILL SWITCH ON — all buys halted. /resume to clear.")

    async def run(self) -> None:
        try:
            await self.dp.start_polling(self.bot, handle_signals=False)
        except Exception as e:
            logger.error("telegram control loop crashed: {}", e)


async def run_control_bot(db, state, notifier) -> None:
    s = get_settings()
    if not s.telegram_bot_token or not s.telegram_chat_id:
        logger.warning("telegram not configured; control bot disabled")
        return
    bot = ControlBot(db, state, notifier)
    await bot.run()
