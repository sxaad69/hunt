from __future__ import annotations

import time
from typing import Optional

import httpx
from loguru import logger

from hunt.config import WSOL, get_settings
from hunt.db.database import Database
from hunt.exec.jupiter import JupiterClient
from hunt.notify.base import Notifier
from hunt.scout.dexscreener import DexScreener
from hunt.utils.solana import load_keypair
from hunt.watch.parser import TradeSignal


class TradeExecutor:
    def __init__(self, db: Database, notifier: Notifier) -> None:
        self.s = get_settings()
        self.db = db
        self.notifier = notifier
        self.http = httpx.AsyncClient()
        self.ds = DexScreener(self.http)
        self.jup = JupiterClient(self.http)
        self._sol_price: tuple[float, float] = (0.0, 0.0)

    @property
    def mode(self) -> str:
        return "PAPER" if self.s.dry_run else "LIVE"

    async def sol_price_usd(self) -> float:
        now = time.time()
        if now - self._sol_price[1] > 60:
            price, _ = await self.ds.price_for_mint(WSOL)
            if price > 0:
                self._sol_price = (price, now)
        return self._sol_price[0]

    async def copy_buy(self, sig: TradeSignal) -> None:
        size_sol = self.s.trade_size_sol
        symbol = await self._symbol(sig.mint)
        if self.mode == "PAPER":
            await self._paper_buy(sig, size_sol, symbol)
        else:
            await self._live_buy(sig, size_sol, symbol)

    async def _symbol(self, mint: str) -> str | None:
        pairs = await self.ds.pairs_for_mints([mint])
        ht = pairs.get(mint)
        return ht.symbol if ht else None

    async def _paper_buy(self, sig: TradeSignal, size_sol: float, symbol: str | None) -> None:
        pairs = await self.ds.pairs_for_mints([sig.mint])
        ht = pairs.get(sig.mint)
        if not ht or ht.price_usd <= 0:
            await self.db.insert_trade(
                self.mode, "BUY", sig.mint, symbol, size_sol, None, None,
                None, status="no_price",
            )
            return
        sol_usd = await self.sol_price_usd()
        if sol_usd <= 0:
            return
        usd_in = size_sol * sol_usd
        tokens = usd_in / ht.price_usd
        pos_id = await self.db.create_position(
            sig.mint, symbol, "PAPER", size_sol, tokens, ht.price_usd,
            self.s.take_profit_pct, self.s.stop_loss_pct, self.s.trailing_stop_pct,
        )
        await self.db.insert_trade(
            self.mode, "BUY", sig.mint, symbol, size_sol, tokens,
            ht.price_usd, pos_id, signature=sig.signature[:32],
        )
        await self.notifier.send(
            f"🟢 [{self.mode}] BUY ${symbol or sig.mint[:6]} {size_sol} SOL "
            f"@ ${ht.price_usd:.6g} (whale {sig.wallet[:6]}… spent {sig.sol_amount:.2f} SOL)"
        )

    async def _live_buy(self, sig: TradeSignal, size_sol: float, symbol: str | None) -> None:
        kp = load_keypair()
        if not kp:
            logger.error("live buy skipped: no wallet key")
            return
        quote = await self.jup.quote(WSOL, sig.mint, int(size_sol * 1e9))
        if not quote:
            await self.db.insert_trade(self.mode, "BUY", sig.mint, symbol, size_sol, None, None, None, status="quote_fail")
            return
        tx_b64 = await self.jup.build_swap_transaction(quote, str(kp.pubkey()))
        if not tx_b64:
            await self.db.insert_trade(self.mode, "BUY", sig.mint, symbol, size_sol, None, None, None, status="build_fail")
            return
        signature = await self.jup.sign_and_send(tx_b64, kp)
        tokens_out = quote.out_amount_raw / 1e6
        ok = signature is not None
        pos_id = await self.db.create_position(
            sig.mint, symbol, "LIVE", size_sol, tokens_out if ok else 0.0, None,
            self.s.take_profit_pct, self.s.stop_loss_pct, self.s.trailing_stop_pct,
        )
        await self.db.insert_trade(
            self.mode, "BUY", sig.mint, symbol, size_sol, tokens_out if ok else None,
            None, pos_id, signature=signature, status="ok" if ok else "send_fail",
        )
        await self.notifier.send(
            f"🟢 [LIVE] BUY ${symbol or sig.mint[:6]} {size_sol} SOL — {signature or 'FAILED'}"
        )

    async def copy_sell(self, sig: TradeSignal, fraction: float) -> None:
        mode = self.mode
        pos = await self.db.open_position_for_mint(sig.mint, mode)
        if not pos:
            return
        symbol = pos["symbol"]
        sell_fraction = min(1.0, max(fraction, 0.1))
        if self.mode == "PAPER":
            await self._paper_sell(pos, sell_fraction, reason="whale_sell")
        else:
            await self._live_sell(pos, sell_fraction)

    async def _paper_sell(self, pos, fraction: float, reason: str) -> None:
        pairs = await self.ds.pairs_for_mints([pos["mint"]])
        ht = pairs.get(pos["mint"])
        price = ht.price_usd if ht and ht.price_usd > 0 else 0.0
        if price <= 0:
            return
        sol_usd = await self.sol_price_usd()
        if sol_usd <= 0:
            return
        tokens_to_sell = pos["tokens"] * fraction
        exit_sol = tokens_to_sell * price / sol_usd
        remaining_tokens = pos["tokens"] - tokens_to_sell

        if fraction >= 0.999 or remaining_tokens * price / sol_usd < 0.005:
            await self.db.close_position(pos["id"], exit_sol, reason)
            pnl = exit_sol - pos["size_sol"] * fraction
            emoji = "🔴" if pnl < 0 else "🟣"
            await self.notifier.send(
                f"{emoji} [{self.mode}] SELL ${pos['symbol'] or '?'} x{fraction:.0%} → "
                f"{exit_sol:+.4f} SOL ({reason})"
            )
        else:
            await self.db.db.execute(
                "UPDATE positions SET tokens=?, size_sol=size_sol-? WHERE id=?",
                (remaining_tokens, pos["size_sol"] * fraction, pos["id"]),
            )
            await self.db.db.commit()

    async def _live_sell(self, pos, fraction: float) -> None:
        kp = load_keypair()
        if not kp:
            logger.error("live sell skipped: no wallet key")
            return
        tokens_raw = int(pos["tokens"] * fraction)
        if tokens_raw <= 0:
            return
        quote = await self.jup.quote(pos["mint"], WSOL, tokens_raw)
        if not quote:
            await self.db.insert_trade(self.mode, "SELL", pos["mint"], pos["symbol"], None, tokens_raw, None, pos["id"], status="quote_fail")
            return
        tx_b64 = await self.jup.build_swap_transaction(quote, str(kp.pubkey()))
        if not tx_b64:
            return
        signature = await self.jup.sign_and_send(tx_b64, kp)
        exit_sol = quote.out_amount_raw / 1e9
        if signature and fraction >= 0.999:
            await self.db.close_position(pos["id"], exit_sol, "whale_sell")
        await self.notifier.send(
            f"🔴 [LIVE] SELL ${pos['symbol'] or '?'} x{fraction:.0%} — {signature or 'FAILED'}"
        )

    async def exit_position(self, pos, reason: str) -> None:
        if self.mode == "PAPER":
            await self._paper_sell(pos, 1.0, reason)
        else:
            await self._live_sell(pos, 1.0)
