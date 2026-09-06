from __future__ import annotations

from hunt.utils.swaps import (
    AccountDeltas,
    extract_swap_for_owner,
    extract_swaps_from_helius_history,
)

WSOL = "So11111111111111111111111111111111111111112"
MEME = "MemeMint11111111111111111111111111111111111"


def test_buy_detection():
    d = AccountDeltas()
    d.token_deltas[("whale", MEME)] = 500.0
    d.token_deltas[("whale", WSOL)] = -1.25
    swaps = extract_swap_for_owner(d, "whale")
    assert len(swaps) == 1
    s = swaps[0]
    assert s.side == "BUY"
    assert s.mint == MEME
    assert abs(s.sol_amount - 1.25) < 1e-9


def test_sell_detection():
    d = AccountDeltas()
    d.token_deltas[("whale", MEME)] = -300.0
    d.token_deltas[("whale", WSOL)] = 0.8
    swaps = extract_swap_for_owner(d, "whale")
    assert len(swaps) == 1
    assert swaps[0].side == "SELL"
    assert abs(swaps[0].token_amount - 300.0) < 1e-9


def test_other_owner_ignored():
    d = AccountDeltas()
    d.token_deltas[("someone_else", MEME)] = 999.0
    d.token_deltas[("someone_else", WSOL)] = -5.0
    assert extract_swap_for_owner(d, "whale") == []


def test_usdc_quote():
    USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    d = AccountDeltas()
    d.token_deltas[("whale", MEME)] = 100.0
    d.token_deltas[("whale", USDC)] = -250.0
    swaps = extract_swap_for_owner(d, "whale")
    assert len(swaps) == 1
    assert swaps[0].quote_mint == USDC
    assert abs(swaps[0].sol_amount - 250.0) < 1e-9


def test_helius_history_extraction():
    item = {
        "signature": "abc",
        "timestamp": 1700000000,
        "tokenTransfers": [
            {"fromUserAccount": "whale", "toUserAccount": "pool", "mint": WSOL, "tokenAmount": 2.0},
            {"fromUserAccount": "pool", "toUserAccount": "whale", "mint": MEME, "tokenAmount": 800.0},
        ],
        "nativeTransfers": [],
    }
    out = extract_swaps_from_helius_history(item, "whale")
    assert len(out) == 1
    t = out[0]
    assert t.side == "BUY"
    assert abs(t.sol_amount - 2.0) < 1e-9
    assert abs(t.token_amount - 800.0) < 1e-9
