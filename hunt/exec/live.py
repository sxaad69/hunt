"""Live Solana execution for the moonshot engine.

The paper engine writes SQLite; this module is what HUNT_DRY_RUN=false unlocks.
Every entry/exit returns ACTUAL fills parsed from the confirmed transaction
(post-token-balances), not assumptions — so position PnL in LIVE mode is real.

Venue logic:
  * buy  — try the PumpFun bonding curve first (species-A, still-curve coins).
           If the curve is complete (graduated) or the txn reverts, fall back to
           the AMM via Jupiter (PumpSwap pools; Jupiter routes pAMMBay).
  * sell — same order: curve sell, and on graduation -> Jupiter AMM sell.

Bonding-curve instructions come from the vendored `hunt.exec.pumpfun` package
(protocol-exact, unsigned; signing happens here with the bot keypair).

IMPORTANT: this module never runs in paper mode. Callers gate on dry_run.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Optional

import httpx
from loguru import logger
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from hunt.config import WSOL, get_settings
from hunt.exec.pumpfun import (
    PumpFunError,
    build_buy,
    build_create_ata_idempotent,
    build_message,
    build_sell,
    fetch_latest_blockhash,
)
from hunt.exec.pumpfun.constants import (
    SYSTEM_PROGRAM,
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
)
from hunt.exec.pumpfun.pda import get_associated_token_address, get_bonding_curve_pda
from hunt.utils.solana import load_keypair


@dataclass(slots=True)
class LiveFill:
    tokens_raw: int            # tokens actually reserved (0 = failed)
    sol_lamports: int          # SOL actually moved (invested or received)
    venue: str                 # "curve" | "amm"
    signature: Optional[str]
    decimals: int = 6
    expected: Optional[int] = None   # plan expectation (sanity)
    gap_bps: int = 0           # (sells) expected-vs-actual shortfall, basis points

    @property
    def ok(self) -> bool:
        return bool(self.signature)

    @property
    def tokens(self) -> float:
        return self.tokens_raw / 10 ** self.decimals if self.tokens_raw > 0 else 0.0


@dataclass(slots=True)
class BuyResult(LiveFill):
    pass


@dataclass(slots=True)
class SellResult(LiveFill):
    pass


class LiveExecutor:
    """Wallet-backed executor. Constructed ONLY when dry_run is False."""

    def __init__(self) -> None:
        self.s = get_settings()
        self.kp: Keypair | None = load_keypair()
        self.rpc = self.s.rpc_http
        self.http = httpx.AsyncClient(timeout=20)
        self.wallet = str(self.kp.pubkey()) if self.kp else None

    # ------------------------------------------------------------------ utils
    async def balance_sol(self) -> float:
        from solana.rpc.async_api import AsyncClient
        async with AsyncClient(self.s.rpc_http) as rpc:
            lamports = (await rpc.get_balance(self.kp.pubkey())).value
            return lamports / 1e9

    async def _raw_rpc(self, method: str, params: list) -> dict | None:
        try:
            r = await self.http.post(self.rpc, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            j = r.json()
            return j.get("result") if j.get("error") is None else None
        except Exception as e:
            logger.debug("raw rpc {} failed: {}", method, e)
            return None

    async def _token_balance_raw(self, mint: str, owner: Pubkey | None = None) -> int:
        owner = owner or self.kp.pubkey()
        result = await self._raw_rpc("getTokenAccountsByOwner",
                                     [str(owner), {"mint": mint}, {"encoding": "jsonParsed"}])
        for acc in (result or {}).get("value") or []:
            parsed = acc.get("account", {}).get("data", {}).get("parsed")
            if parsed and parsed.get("info", {}).get("tokenAmount") is not None:
                return int(parsed["info"]["tokenAmount"]["amount"])
        return 0

    async def _token_decimals(self, mint: str) -> int:
        result = await self._raw_rpc("getTokenSupply", [mint])
        try:
            return int(result["value"]["decimals"])
        except (TypeError, KeyError):
            return 6

    # -------------------------------------------------------------- signing
    async def _sign_send_instructions(self, instructions: list, blockhash: str = "") -> Optional[str]:
        from solders.hash import Hash
        from solana.rpc.async_api import AsyncClient
        last_err = None
        for attempt in range(2):
            try:
                if not blockhash or attempt > 0:
                    async with AsyncClient(self.rpc) as rpc:
                        h = await rpc.get_latest_blockhash()
                    blockhash = str(h.value.blockhash)
                msg = build_message(self.kp.pubkey(), instructions, Hash.from_string(blockhash))
                tx = VersionedTransaction(msg, [self.kp])
                async with AsyncClient(self.rpc) as rpc:
                    resp = await rpc.send_raw_transaction(bytes(tx))
                    await rpc.confirm_transaction(resp.value)
                    return str(resp.value)
            except Exception as e:
                last_err = e
                logger.warning("sign/send attempt {} failed: {}", attempt + 1, e)
                await asyncio.sleep(1.0)
        logger.error("sign/send failed after retries: {}", last_err)
        return None

    # -------------------------------------------------------------- parsing
    async def _snapshot(self, mint: str) -> "tuple[int, int]":
        """(sol_lamports, token_raw) of the wallet right now. Robust to Jupiter's
        versioned txs + ALTs (get_transaction account_keys doesn't always include
        the signer). Buy/sell measure TRUE deltas around the confirmed fill."""
        bal = await self._raw_rpc("getBalance", [str(self.kp.pubkey())])
        sol = int((bal or {}).get("value") or 0)
        tok = await self._token_balance_raw(mint)
        return sol, tok

    async def _curve_quote_mint(self, mint: str) -> Optional[str]:
        """Read the bonding-curve account's quote mint (offset 83..115, the modern
        generation field). Returns the quote-mint address, or 'None' reserved for
        WSOL-quoted curves (zeros at that offset). None if account unreadable."""
        import base64

        curve = get_bonding_curve_pda(Pubkey.from_string(mint))
        result = await self._raw_rpc("getAccountInfo", [str(curve), {"encoding": "base64"}])
        value = (result or {}).get("value")
        if not value:
            return None
        data = base64.b64decode(value["data"][0])
        if len(data) < 115:
            return None
        q = data[83:115]
        if q == b"\x00" * 32:
            return WSOL
        return str(Pubkey(bytes(q)))

    async def _ensure_ata(self, mint_pk: Pubkey, token_program: Pubkey) -> bool:
        """Idempotent ATA create for the wallet. Returns True if it exists or was
        created, False on failure. One small tx (rent ~0.002 SOL)."""
        ata = get_associated_token_address(self.kp.pubkey(), mint_pk, token_program)
        info = await self._raw_rpc("getAccountInfo", [str(ata), {"encoding": "jsonParsed"}])
        if (info or {}).get("value"):
            return True
        ix = build_create_ata_idempotent(self.kp.pubkey(), mint_pk, ata, token_program)
        sig = await self._sign_send_instructions([ix])
        if not sig:
            return False
        re = await self._raw_rpc("getAccountInfo", [str(ata), {"encoding": "jsonParsed"}])
        return bool((re or {}).get("value"))

    async def ensure_atas(
        self, mint: str, quote_mint: str | None = None,
    ) -> tuple[bool, Optional[str]]:
        """Pre-create every token ATA the wallet needs to trade `mint` so the
        Jupiter swap route can fit under the 1232-byte wire cap (a missing ATA
        makes Jupiter pack an ATA-create into the swap -> oversized -> no fill).

        Returns (ok, quote_mint). quote_mint is lazily resolved from the curve
        when not given (WSOL for legacy/quoted-free curves).
        """
        import base64 as _b64

        mint_pk = Pubkey.from_string(mint)
        if quote_mint is None:
            quote_mint = await self._curve_quote_mint(mint)
        qm = quote_mint or WSOL

        # token program for the coin:
        mint_acct = await self._raw_rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        owner = "Unknown"
        try:
            owner = ((mint_acct or {}).get("value") or {}).get("owner", "")
        except Exception:
            pass
        mint_tp = TOKEN_2022_PROGRAM if owner == str(TOKEN_2022_PROGRAM) else TOKEN_PROGRAM
        quote_tp = TOKEN_PROGRAM if qm in (WSOL, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v") else TOKEN_2022_PROGRAM

        created = True
        for m, tp in ((mint, mint_tp), (qm, quote_tp), (WSOL, TOKEN_PROGRAM)):
            try:
                if not await self._ensure_ata(Pubkey.from_string(m), tp):
                    created = False
            except Exception as e:
                logger.warning("ensure_ata failed {} ({}): {}", m[:8], tp[:8], e)
                created = False
        return created, qm

    # ------------------------------------------------------------------ BUY
    async def buy(self, mint: str, size_sol: float, slippage_bps: int | None = None) -> Optional[BuyResult]:
        """Buy `size_sol` SOL of `mint`.

        Venue order (evidence 2026-09-10):
          1. AMM via Jupiter — Jupiter routes the pump bonding curve itself for
             un-graduated coins (exotic/USDC quote mints included), emitting the
             correct current-program CPI. This is the primary path for ALL coins.
          2. Direct pump curve buy — fallback ONLY when the curve is WSOL-quoted
             (the vendored 18-account builder spends SOL; anything else reverts
             with UnsupportedQuoteMint/6063).
        Pre-creates the wallet's ATAs (coin + quote + WSOL) so the Jupiter swap
        fits under the 1232-byte wire cap; a missing ATA inflates the tx over
        the cap and Jupiter silently refuses to build it (the 09-10 hole).

        Returns None if all attempts fail (caller should NOT open a position).
        """
        slippage = slippage_bps or self.s.slippage_bps
        lamports = int(size_sol * 1e9)
        decimals = await self._token_decimals(mint)
        attempts = max(1, int(self.s.buy_retries or 1))
        quote_mint = await self._curve_quote_mint(mint)
        ok_atas, quote_mint = await self.ensure_atas(mint, quote_mint)
        if not ok_atas:
            logger.warning("ATA pre-create incomplete — proceeding anyway for {}", mint[:8])

        for attempt in range(attempts):
            for venue in ("amm", "curve"):
                try:
                    if venue == "amm":
                        r = await self._buy_amm(mint, lamports, slippage)
                    else:
                        # direct all the WSOL/legacy curves; skip for exotic quotes
                        # AND for graduated/closed curves (None) where the direct
                        # builder cannot work — Jupiter handles those too.
                        if quote_mint and quote_mint != WSOL:
                            logger.info("skip direct curve buy {} (quote {}) — using Jupiter",
                                        mint[:8], quote_mint[:8])
                            r = None
                        elif quote_mint is None:
                            r = None
                        else:
                            r = await self._buy_curve(mint, lamports, slippage)
                    if r and r.ok:
                        r.decimals = decimals
                        return r
                    elif venue == "curve" and r is None:
                        logger.warning("direct curve buy unavailable {} (quote {})", mint[:8], quote_mint or "WSOL")
                except Exception as e:
                    logger.warning("live buy {} path failed: {}", venue, e)
            if attempt + 1 < attempts:
                logger.info("live buy retry {}/{} for {}", attempt + 2, attempts, mint[:8])
                await asyncio.sleep(0.5 * (attempt + 1))
        logger.error("live buy failed both venues for {} (quote {})", mint[:8], quote_mint or "WSOL")
        return None

    async def _buy_curve(self, mint: str, sol_lamports: int, slippage_bps: int) -> Optional[BuyResult]:
        pre_sol, pre_tok = await self._snapshot(mint)
        try:
            plan = await build_buy(self.rpc, self.kp.pubkey(), mint, sol_lamports,
                                   slippage_bps=slippage_bps, http_client=self.http)
        except PumpFunError as e:
            logger.info("curve buy unavailable ({}): using AMM", e)
            return None
        sig = await self._sign_send_instructions(plan.instructions)
        if not sig:
            return None
        post_sol, post_tok = await self._snapshot(mint)
        if post_tok <= pre_tok:
            # confirmed tx but no tokens arrived (failed tx still yields a
            # signature) — NEVER phantom-fill from the quote.
            logger.warning("curve buy no tokens moved — treating as failed {}", mint[:8])
            return None
        sol_spent = max(0, pre_sol - post_sol) or sol_lamports
        return BuyResult(tokens_raw=post_tok or plan.expected_tokens,
                         sol_lamports=sol_spent,
                         venue="curve", signature=sig, expected=plan.expected_tokens)

    async def _buy_amm(self, mint: str, sol_lamports: int, slippage_bps: int) -> Optional[BuyResult]:
        from hunt.exec.jupiter import JupiterClient
        jup = JupiterClient(self.http)
        quote = await jup.quote(WSOL, mint, sol_lamports, slippage_bps=slippage_bps)
        if not quote:
            logger.info("jup AMM buy: no route for {}", mint[:8])
            return None
        tx_b64 = await jup.build_swap_transaction(quote, self.wallet)
        if not tx_b64:
            logger.warning("jup swap build failed for {} — route {} bytes over/veto?",
                           mint[:8], quote.out_amount_raw)
            return None
        pre_sol, pre_tok = await self._snapshot(mint)
        sig = await jup.sign_and_send(tx_b64, self.kp)
        if not sig:
            return None
        post_sol, post_tok = await self._snapshot(mint)
        if post_tok <= pre_tok:
            logger.warning("amm buy no tokens moved — treating as failed {}", mint[:8])
            return None
        sol_spent = max(0, pre_sol - post_sol) or sol_lamports
        return BuyResult(tokens_raw=post_tok or quote.out_amount_raw,
                         sol_lamports=sol_spent,
                         venue="amm", signature=sig, expected=quote.out_amount_raw)

    # ------------------------------------------------------------------ SELL
    async def sell(self, mint: str, tokens_raw: int, slippage_bps: int | None = None,
                   close_ata: bool = True) -> Optional[SellResult]:
        """Sell exactly `tokens_raw` raw units.

        Venue order (evidence 2026-09-10):
          1. AMM via Jupiter — routes both curve (un-graduated, payoff goes to
             the quote mint) and graduated coins. Primary path.
          2. Direct pump curve sell — fallback for WSOL-quoted curves.
        Returns None if both venues fail — caller keeps the tokens.

        close_ata: only True for full-remainder sells. SPL CloseAccount reverts
        the whole tx on a partial balance, so tier slices must pass False."""
        if tokens_raw <= 0:
            return None
        slippage = slippage_bps or self.s.slippage_bps
        decimals = await self._token_decimals(mint)
        attempts = max(1, int(self.s.sell_retries or 1))
        guard_bps = int(self.s.sell_gap_guard_bps or 0)
        quote_mint = await self._curve_quote_mint(mint)
        await self.ensure_atas(mint, quote_mint)
        for attempt in range(attempts):
            for venue in ("amm", "curve"):
                try:
                    if venue == "amm":
                        r = await self._sell_amm(mint, tokens_raw, slippage)
                    else:
                        if quote_mint and quote_mint != WSOL:
                            logger.info("skip direct curve sell {} (quote {}) — using Jupiter",
                                        mint[:8], quote_mint[:8])
                            r = None
                        else:
                            r = await self._sell_curve(mint, tokens_raw, slippage, close_ata)
                    if r and r.ok:
                        r.decimals = decimals
                        if r.expected and r.expected > 0 and guard_bps > 0:
                            gap = int((r.expected - r.sol_lamports) * 10000 / r.expected)
                            r.gap_bps = max(0, gap)
                            if r.gap_bps > guard_bps:
                                logger.warning("live sell fill gap {}bps (expected {} got {}) {} {}",
                                               r.gap_bps, r.expected, r.sol_lamports, venue, mint[:8])
                        return r
                except Exception as e:
                    logger.warning("live sell {} path failed: {}", venue, e)
            if attempt + 1 < attempts:
                logger.info("live sell retry {}/{} for {}", attempt + 2, attempts, mint[:8])
                await asyncio.sleep(0.5 * (attempt + 1))
        return None

    async def _sell_curve(self, mint: str, token_amount: int, slippage_bps: int,
                          close_ata: bool = True) -> Optional[SellResult]:
        pre_sol, pre_tok = await self._snapshot(mint)
        try:
            plan = await build_sell(self.rpc, self.kp.pubkey(), mint, token_amount,
                                    slippage_bps=slippage_bps, http_client=self.http)
        except PumpFunError as e:
            logger.info("curve sell unavailable ({}): using AMM", e)
            return None
        ixs = plan.instructions
        if not close_ata and len(ixs) > 1:
            ixs = ixs[:1]  # drop build_close_account — would revert a partial sell
        sig = await self._sign_send_instructions(ixs)
        if not sig:
            return None
        post_sol, post_tok = await self._snapshot(mint)
        tok_sold = pre_tok - post_tok
        if tok_sold <= 0:
            logger.warning("curve sell no tokens moved — treating as failed {}", mint[:8])
            return None
        sol_in = post_sol - pre_sol
        return SellResult(tokens_raw=min(0, -tok_sold), sol_lamports=max(0, sol_in) or plan.expected_sol_out,
                          venue="curve", signature=sig, expected=plan.expected_sol_out)

    async def _sell_amm(self, mint: str, token_amount: int, slippage_bps: int) -> Optional[SellResult]:
        from hunt.exec.jupiter import JupiterClient
        jup = JupiterClient(self.http)
        quote = await jup.quote(mint, WSOL, token_amount, slippage_bps=slippage_bps)
        if not quote:
            logger.info("jup AMM sell: no route for {}", mint[:8])
            return None
        tx_b64 = await jup.build_swap_transaction(quote, self.wallet)
        if not tx_b64:
            return None
        pre_sol, pre_tok = await self._snapshot(mint)
        sig = await jup.sign_and_send(tx_b64, self.kp)
        if not sig:
            return None
        post_sol, post_tok = await self._snapshot(mint)
        tok_sold = pre_tok - post_tok
        if tok_sold <= 0:
            logger.warning("amm sell no tokens moved — treating as failed {}", mint[:8])
            return None
        sol_in = post_sol - pre_sol
        return SellResult(tokens_raw=min(0, -tok_sold), sol_lamports=max(0, sol_in) or quote.out_amount_raw,
                          venue="amm", signature=sig, expected=quote.out_amount_raw)


# --------------------------------------------------------------- module state
_LIVE_EXEC: LiveExecutor | None = None


def get_live_executor() -> LiveExecutor | None:
    global _LIVE_EXEC
    if _LIVE_EXEC is not None:
        return _LIVE_EXEC
    if get_settings().dry_run:
        return None
    _LIVE_EXEC = LiveExecutor()
    return _LIVE_EXEC


def live_enabled() -> bool:
    return not get_settings().dry_run


# --------------------------------------------------------------------- CLI
async def _cli_balance() -> None:
    ex = LiveExecutor()
    if not ex.kp:
        print("HUNT_WALLET_PRIVATE_KEY not set")
        return
    print(f"wallet: {ex.wallet}")
    print(f"SOL balance: {await ex.balance_sol():.6f}")


async def _cli_smoke(mint: str, size_sol: float) -> None:
    """Smoke test: buy `size_sol` SOL of a GRADUATED mint via the AMM, then sell it back."""
    ex = LiveExecutor()
    if not ex.kp:
        raise SystemExit("HUNT_WALLET_PRIVATE_KEY not set")
    print(f"balance before: {await ex.balance_sol():.6f} SOL")
    r = await ex._buy_amm(mint, int(size_sol * 1e9), 1000)
    print(f"buy: {r}")
    if not r or not r.ok:
        raise SystemExit("SMOKE FAILED (buy)")
    bal = await ex._token_balance_raw(mint)
    print(f"tokens held: {bal} (raw)")
    r2 = await ex.sell(mint, bal)
    print(f"sell: {r2}")
    if not r2 or not r2.ok:
        raise SystemExit("SMOKE FAILED (sell)")
    print(f"balance after: {await ex.balance_sol():.6f} SOL")
    print("SMOKE OK")


async def _cli_send(to_address: str, amount_sol: float, send_all: bool) -> None:
    """Withdraw SOL from the bot wallet to ANY Solana address. amount_sol=0 +
    --all sends the full balance (minus fee). Prints the tx signature and the
    remaining balance. This is the operator's money-out path."""
    ex = LiveExecutor()
    if not ex.kp:
        raise SystemExit("HUNT_WALLET_PRIVATE_KEY not set")
    from solders.pubkey import Pubkey
    to = Pubkey.from_string(to_address)
    bal_lamports = int(await ex.balance_sol() * 1e9)
    fee_lamports = 5000
    if send_all:
        lamports = bal_lamports - fee_lamports
    else:
        lamports = int(amount_sol * 1e9)
    if lamports <= 0 or lamports > bal_lamports - fee_lamports:
        raise SystemExit(f"invalid amount: bal={bal_lamports/1e9:.6f} SOL")
    from solders.system_program import TransferParams, transfer
    from solders.message import Message
    from solders.transaction import Transaction
    from hunt.exec.pumpfun import fetch_latest_blockhash
    blockhash = await fetch_latest_blockhash(ex.rpc, http_client=ex.http)
    ix = transfer(TransferParams(from_pubkey=ex.kp.pubkey(), to_pubkey=to, lamports=lamports))
    msg = Message.new_with_blockhash(ix, ex.kp.pubkey(), blockhash)
    tx = Transaction([ex.kp], msg, blockhash)
    from solana.rpc.async_api import AsyncClient
    async with AsyncClient(ex.rpc) as rpc:
        resp = await rpc.send_transaction(tx)
        sig = str(resp.value)
        await rpc.confirm_transaction(resp.value)
    print(f"sent {lamports/1e9:.6f} SOL -> {to_address}")
    print(f"signature: {sig}")
    print(f"remaining balance: {await ex.balance_sol():.6f} SOL")


def main() -> None:
    p = argparse.ArgumentParser(prog="hunt.exec.live")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("balance")
    s = sub.add_parser("smoke")
    s.add_argument("mint")
    s.add_argument("--size-sol", type=float, default=0.001)
    w = sub.add_parser("send")
    w.add_argument("to")
    w.add_argument("amount_sol", type=float, nargs="?", default=0.0)
    w.add_argument("--all", action="store_true", help="send full balance minus fee")
    args = p.parse_args()
    if args.cmd == "balance":
        asyncio.run(_cli_balance())
    elif args.cmd == "smoke":
        asyncio.run(_cli_smoke(args.mint, args.size_sol))
    else:
        asyncio.run(_cli_send(args.to, args.amount_sol, args.all))


if __name__ == "__main__":
    main()