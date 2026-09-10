"""_live_exit_price: fallback-first, DexScreener last-resort for LIVE only,
paper path unchanged (0 = skip). Fakes only — no network, no money.
"""
import asyncio

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


def _run_with(fallback_px, live, ds):
    async def fake_fallback(client, mint):
        return fallback_px

    orig = runmod._price_for_mint_fallback
    runmod._price_for_mint_fallback = fake_fallback
    try:
        return asyncio.run(runmod._live_exit_price(None, ds, "MINT", live))
    finally:
        runmod._price_for_mint_fallback = orig


def test_fallback_wins_ds_untouched():
    ds = FakeDS(px=9.9)
    assert _run_with(1.5, True, ds) == 1.5
    assert ds.calls == 0


def test_live_falls_back_to_dex():
    ds = FakeDS(px=2.5)
    assert _run_with(0.0, True, ds) == 2.5
    assert ds.calls == 1


def test_paper_never_touches_dex():
    ds = FakeDS(px=2.5)
    assert _run_with(0.0, False, ds) == 0.0
    assert ds.calls == 0


def test_live_dex_failure_returns_zero():
    ds = FakeDS(exc=RuntimeError("down"))
    assert _run_with(0.0, True, ds) == 0.0
