from __future__ import annotations

from hunt.score.metrics import compute_metrics
from hunt.utils.swaps import WalletTradeLike


def _trades(specs: list[tuple[str, int, str, float, float]]) -> list[WalletTradeLike]:
    return [
        WalletTradeLike(signature=f"sig{i}", ts=1700000000 + i * 10, mint=m, side=side,
                        sol_amount=sol, token_amount=tok)
        for i, (m, _, side, sol, tok) in enumerate(
            (s[0], 0, s[1], s[2], s[3]) for s in specs
        )
    ]


def test_profitable_wallet_qualifies():
    trades = []
    ts = 1700000000
    sig_n = 0
    for i in range(36):
        mint = f"mint{i % 6}"
        trades.append(WalletTradeLike(f"s{sig_n}", ts, mint, "BUY", 1.0, 1000.0)); sig_n += 1
        trades.append(WalletTradeLike(f"s{sig_n}", ts + 600, mint, "SELL", 2.0, 1000.0)); sig_n += 1
    m = compute_metrics(trades)
    assert m.trades == 36
    assert m.win_rate == 1.0
    assert m.realized_pnl_sol == 36.0
    assert m.distinct_tokens == 6
    assert m.qualified is True
    assert m.score > 0.5


def test_losing_wallet_rejected():
    trades = []
    ts = 1700000000
    for i in range(40):
        mint = f"m{i}"
        trades.append(WalletTradeLike(f"b{i}", ts, mint, "BUY", 1.0, 100.0))
        trades.append(WalletTradeLike(f"s{i}", ts + 3600, mint, "SELL", 0.2, 100.0))
    m = compute_metrics(trades)
    assert m.win_rate == 0.0
    assert m.realized_pnl_sol < 0
    assert m.qualified is False


def test_instant_seller_flagged():
    trades = []
    ts = 1700000000
    for i in range(30):
        mint = f"m{i % 6}"
        trades.append(WalletTradeLike(f"b{i}", ts, mint, "BUY", 1.0, 100.0))
        trades.append(WalletTradeLike(f"s{i}", ts + 5, mint, "SELL", 1.5, 100.0))
    m = compute_metrics(trades)
    assert m.instant_sell_ratio == 1.0
    assert any("instant_sell" in r for r in m.reasons)
    assert m.qualified is False


def test_low_sample_not_qualified():
    trades = [
        WalletTradeLike("b1", 1700000000, "m1", "BUY", 1.0, 100.0),
        WalletTradeLike("s1", 1700003600, "m1", "SELL", 3.0, 100.0),
    ]
    m = compute_metrics(trades)
    assert m.trades == 1
    assert m.qualified is False
    assert any("low_sample" in r for r in m.reasons)


def test_open_bags_counted():
    trades = [
        WalletTradeLike("b1", 1700000000, "m1", "BUY", 1.0, 100.0),
        WalletTradeLike("s1", 1700003600, "m1", "SELL", 3.0, 100.0),
        WalletTradeLike("b2", 1700007200, "m2", "BUY", 0.5, 50.0),
    ]
    m = compute_metrics(trades)
    assert m.open_bags == 1
