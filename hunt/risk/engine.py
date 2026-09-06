from __future__ import annotations

import time

from loguru import logger

from hunt.config import get_settings
from hunt.db.database import Database
from hunt.notify.base import AppState, Notifier
from hunt.watch.parser import TradeSignal


class RiskEngine:
    def __init__(self, db: Database, notifier: Notifier, state: AppState) -> None:
        self.s = get_settings()
        self.db = db
        self.notifier = notifier
        self.state = state
        self._loss_alert_sent_day: str = ""

    async def _daily_loss_exceeded(self, mode: str) -> bool:
        pnl = await self.db.daily_realized_pnl_sol(mode)
        return pnl <= -abs(self.s.daily_loss_limit_sol)

    async def _whale_bag_tokens(self, wallet: str, mint: str) -> float:
        trades = await self.db.get_wallet_trades(wallet)
        bag = 0.0
        for t in trades:
            if t.mint != mint:
                continue
            if t.side == "BUY":
                bag += t.token_amount
            else:
                bag = max(0.0, bag - t.token_amount)
        return bag

    async def evaluate(self, sig: TradeSignal) -> tuple[str, str | None, dict]:
        mode = "PAPER" if self.s.dry_run else "LIVE"

        if sig.side == "BUY":
            return await self._eval_buy(sig, mode)
        return await self._eval_sell(sig, mode)

    async def _eval_buy(self, sig: TradeSignal, mode: str) -> tuple[str, str | None, dict]:
        if self.state.kill_switch:
            return "skip", "kill_switch", {}
        if self.state.paused:
            return "skip", "paused", {}

        if sig.sol_amount < self.s.min_whale_buy_sol:
            return "skip", f"dust:{sig.sol_amount:.3f}", {}

        # global mint cooldown (any wallet) + wallet+mint cooldown
        age = await self.db.last_signal_age_s(sig.wallet, sig.mint)
        if age is not None and age < self.s.cooldown_s:
            return "skip", f"cooldown:{int(age)}s", {}
        # extra: if we recently closed this mint at loss, cooldown longer
        # check last closed trade for mint
        try:
            import time as _t
            rows = await self.db._db.execute("SELECT closed_ts FROM positions WHERE mint=? AND status='closed' ORDER BY closed_ts DESC LIMIT 1")
            r = await rows.fetchone()
            if r and r[0] and _t.time() - r[0] < self.s.cooldown_s:
                return "skip", f"mint_cooldown:{int(_t.time()-r[0])}s", {}
        except Exception:
            pass

        if await self.db.open_position_for_mint(sig.mint, mode):
            return "skip", "already_holding", {}

        if await self.db.count_open_positions(mode) >= self.s.max_open_positions:
            return "skip", "max_positions", {}

        # max_total_exposure: Σ size_sol
        try:
            stats = await self.db.total_stats()
            open_sol = float(stats.get("open_sol", 0) or 0)
            # cap at max_open * trade_size (0.4 SOL default) plus buffer
            max_exposure = self.s.max_open_positions * self.s.trade_size_sol
            if open_sol >= max_exposure:
                return "skip", f"max_exposure:{open_sol:.3f}", {}
        except Exception:
            pass

        if await self._daily_loss_exceeded(mode):
            today = time.strftime("%Y-%m-%d")
            if self._loss_alert_sent_day != today:
                self._loss_alert_sent_day = today
                await self.notifier.send(
                    f"🛑 daily loss limit hit ({self.s.daily_loss_limit_sol} SOL) — buys halted until tomorrow"
                )
            return "skip", "daily_loss_limit", {}

        checks = await self._token_checks(sig.mint)
        if checks is None:
            return "skip", "no_market_data", {}
        price_usd, liquidity_usd, market_cap, created_at = checks
        if liquidity_usd < self.s.min_liquidity_usd:
            return "skip", f"thin_liquidity:${liquidity_usd:.0f}", {}
        if market_cap and market_cap < self.s.min_market_cap_usd:
            return "skip", f"low_mc:${market_cap:.0f}", {}
        if (
            self.s.max_token_age_min > 0
            and created_at
            and (time.time() - created_at) > self.s.max_token_age_min * 60
        ):
            return "skip", "too_old", {}

        return "copy_buy", None, {"price_usd": price_usd}

    async def _token_checks(self, mint: str) -> tuple[float, float, float, int | None] | None:
        from hunt.scout.dexscreener import DexScreener

        if not hasattr(self, "_ds"):
            import httpx

            self._ds = DexScreener(httpx.AsyncClient())
        try:
            pairs = await self._ds.pairs_for_mints([mint])
            ht = pairs.get(mint)
            if not ht:
                return None
            market_cap = getattr(ht, "market_cap", 0.0) or 0.0
            return ht.price_usd, ht.liquidity_usd, market_cap, ht.pair_created_at
        except Exception as e:
            logger.debug("token check failed {}: {}", mint[:8], e)
            return None

    async def _eval_sell(self, sig: TradeSignal, mode: str) -> tuple[str, str | None, dict]:
        pos = await self.db.open_position_for_mint(sig.mint, mode)
        if not pos:
            return "skip", "no_position", {}

        bag = await self._whale_bag_tokens(sig.wallet, sig.mint)
        pre_sell_bag = bag + sig.token_amount
        fraction = 1.0
        if pre_sell_bag > 0:
            fraction = min(1.0, sig.token_amount / pre_sell_bag)
        if fraction < 0.05:
            return "skip", "negligible_sell", {}
        return "copy_sell", None, {"fraction": fraction}
