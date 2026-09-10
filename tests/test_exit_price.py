"""_live_exit_price: fallback-first, DexScreener next, Jupiter sell-quote last
(LIVE only); quorum uses the LOWEST on disagreement; paper unchanged.
Fakes only — no network, no money.
"""
import asyncio

import hunt.exec.jupiter as jupmod
from hunt.paper import run as runmod


class FakeDS:
    def __init__(self, px=0.0, exc=None):
        self.px = px
        self.exc = exc
        self.calls = 0

    async def price_for_mint(self, mint):
        self.calls += 1
        if self.exc:
            raise self.exc
        return (self.px, 0.0)


class FakeEx:
    def __init__(self, raw=0, dec=6):
        self.raw = raw
        self.dec = dec

    async def _token_balance_raw(self, mint):
        return self.raw

    async def _token_decimals(self, mint):
        return self.dec


class FakeQuote:
    def __init__(self, out):
        self.out_amount_raw = out


class FakeJup:
    def __init__(self, out=0):
        self.out = out

    async def quote(self, in_mint, out_mint, amount_raw):
        return FakeQuote(self.out) if self.out > 0 else None


def _run_with(fallback_px, live, ds, ex=None, sol=0.0):
    async def fake_fallback(client, mint):
        return fallback_px

    orig = runmod._price_for_mint_fallback
    runmod._price_for_mint_fallback = fake_fallback
    runmod._jup_px_cache.clear()
    try:
        return asyncio.run(runmod._live_exit_price(None, ds, "MINT", live, ex, sol))
    finally:
        runmod._price_for_mint_fallback = orig


def test_fallback_and_dex_quorum_takes_min():
    ds = FakeDS(px=9.9)  # wildly over fallback -> disagree -> min wins
    price, _ = _run_with(1.5, True, ds)
    assert price == 1.5
    assert ds.calls == 1  # quorum must consult all sources to detect disagreement


def test_live_falls_back_to_dex():
    ds = FakeDS(px=2.5)
    price, source = _run_with(0.0, True, ds)
    assert (price, source) == (2.5, "dex")
    assert ds.calls == 1


def test_paper_never_touches_dex():
    ds = FakeDS(px=2.5)
    price, source = _run_with(0.0, False, ds)
    assert (price, source) == (0.0, "none")
    assert ds.calls == 0


def test_quorum_uses_lowest_on_disagreement():
    ds = FakeDS(px=1.5)  # 50% over fallback -> disagree -> min wins
    price, _ = _run_with(1.0, True, ds)
    assert price == 1.0


def test_quorum_agreeing_sources_take_min():
    ds = FakeDS(px=1.05)
    price, _ = _run_with(1.0, True, ds)
    assert price == 1.0


def test_live_jupiter_quote_when_all_blind():
    ds = FakeDS(exc=RuntimeError("down"))
    ex = FakeEx(raw=1_000_000, dec=6)
    orig = jupmod.JupiterClient
    jupmod.JupiterClient = lambda client: FakeJup(out=50_000_000)  # 0.05 SOL for 1 token
    runmod._jup_px_cache.clear()
    try:
        price, source = _run_with(0.0, True, ds, ex, sol=100.0)
    finally:
        jupmod.JupiterClient = orig
    assert source == "jup"
    assert abs(price - 5.0) < 1e-9  # 0.05 SOL * $100 / 1 token


def test_live_all_dark_returns_none():
    ds = FakeDS(exc=RuntimeError("down"))
    price, source = _run_with(0.0, True, ds, FakeEx(raw=0), sol=100.0)
    assert (price, source) == (0.0, "none")



class FakeJupQuoteRoute:
    def __init__(self, out_raw, in_raw=10_000_000):
        self.out_amount_raw = out_raw
        self.in_amount_raw = in_raw


class FakeJupProxy:
    def __init__(self, q):
        self._q = q
    async def quote(self, a, b, amt):
        return self._q


def test_jup_entry_price_route_math():
    from hunt.paper import run as r
    import hunt.exec.jupiter as jmod
    orig = jmod.JupiterClient
    jmod.JupiterClient = lambda c: FakeJupProxy(FakeJupQuoteRoute(out_raw=5_000_000_000, in_raw=10_000_000))
    import hunt.exec.live as lv
    orig_gle = lv.get_live_executor
    lv.get_live_executor = lambda: FakeEx(raw=0, dec=6)
    try:
        async def go():
            return await r._jup_entry_price(object(), "MINT", 100.0)
        px = asyncio.run(go())
        # 0.01 SOL in, 5000 tokens out @ dec6 -> $1 for 5000 tk -> $0.0002/tk
        assert abs(px - 0.0002) < 1e-9
    finally:
        jmod.JupiterClient = orig
        lv.get_live_executor = orig_gle


def test_jup_entry_price_no_route_returns_zero():
    from hunt.paper import run as r
    import hunt.exec.jupiter as jmod
    jmod.JupiterClient = lambda c: FakeJupProxy(None)
    try:
        async def go():
            return await r._jup_entry_price(object(), "MINT", 100.0)
        assert asyncio.run(go()) == 0.0
    finally:
        import hunt.exec.jupiter as jj
        jj.JupiterClient = lambda c: FakeJupProxy(None)
