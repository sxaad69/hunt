"""On-chain mcap + circulating top-10 gates. No network."""
import struct

from hunt.paper.onchain_intel import circulating_top10_pct, curve_fdv_sol, protocol_token_accounts
from hunt.paper.run import survival_filter, _fdv_from_payload
from hunt.utils.solanatracker import verdict_from_risk
from hunt.watch.price_feed import amm_price_sol, parse_curve_quote, parse_spl_amount


def _coin(**kw):
    base = {
        "description": "x",
        "image_uri": "x",
        "twitter": "https://x.com/x",
        "telegram": "",
        "website": "",
        "created_timestamp": 1_700_000_000_000,
        "_mcap_ok": True,
        "_graduated": False,
        "market_cap": 80.0,
        "_intel_ok": True,
        "_top10": 40.0,
    }
    base.update(kw)
    return base


def test_curve_fdv_birth_is_about_28_sol():
    vs = 30_000_000_000
    vt = 1_073_000_000_000_000
    supply = 1_000_000_000_000_000
    fdv = curve_fdv_sol(vs, vt, supply)
    assert fdv is not None
    assert 27.0 < fdv < 29.0


def test_curve_fdv_none_when_drained():
    assert curve_fdv_sol(0, 1, 1) is None
    assert curve_fdv_sol(1, 0, 1) is None
    assert curve_fdv_sol(1, 1, 0) is None


def test_circulating_excludes_curve_and_fails_closed():
    assert circulating_top10_pct(0, [], set()) is None
    assert circulating_top10_pct(1000, [], {"curve"}) is None
    curve = "curveATA"
    accs = [
        {"address": curve, "amount": 900},
        {"address": "dev", "amount": 100},
    ]
    pct = circulating_top10_pct(1000, accs, {curve})
    assert pct == 100.0


def test_circulating_spread_holders_not_heavy():
    curve = "curveATA"
    accs = [{"address": curve, "amount": 500}]
    accs += [{"address": f"h{i}", "amount": 50} for i in range(10)]
    pct = circulating_top10_pct(1000, accs, {curve})
    assert pct == 100.0
    accs = [{"address": curve, "amount": 200}]
    accs += [{"address": f"h{i}", "amount": 40} for i in range(20)]
    pct = circulating_top10_pct(1000, accs, {curve})
    assert 49.0 < pct < 51.0


def test_filter_fail_closed_mcap_and_intel():
    ok, reason = survival_filter(_coin(_mcap_ok=False))
    assert (ok, reason) == (False, "mcap_unavailable")
    ok, reason = survival_filter(_coin(market_cap=20))
    assert not ok and reason.startswith("dust_mcap_")
    ok, reason = survival_filter(_coin(market_cap=9000))
    assert not ok and reason.startswith("mcap_ceiling_")
    ok, reason = survival_filter(_coin(_graduated=True, market_cap=80))
    assert ok, reason
    ok, reason = survival_filter(_coin(_graduated=True, market_cap=9000))
    assert ok, reason
    ok, reason = survival_filter(_coin(_graduated=True, market_cap=20))
    assert not ok and reason.startswith("dust_mcap_")
    ok, reason = survival_filter(_coin(_intel_ok=False))
    assert (ok, reason) == (False, "intel_unavailable")
    ok, reason = survival_filter(_coin(_top10=98.8))
    assert not ok and reason.startswith("top10_heavy_")


def test_filter_does_not_trust_indexer_zeros():
    ok, reason = survival_filter(_coin(_intel_ok=False, _top10=0, _snipers=0, _holders=0))
    assert (ok, reason) == (False, "intel_unavailable")


def test_filter_accepts_onchain_clean_curve():
    ok, reason = survival_filter(_coin())
    assert ok, reason


def test_stale_api_timestamp_does_not_kill_live_curve():
    ok, reason = survival_filter(_coin(last_trade_timestamp=1_000_000_000_000, market_cap=106))
    assert ok, reason


def test_parse_curve_quote_fdv_and_graduated():
    buf = bytearray(81)
    struct.pack_into("<Q", buf, 8, 1_073_000_000_000_000)
    struct.pack_into("<Q", buf, 16, 30_000_000_000)
    struct.pack_into("<Q", buf, 40, 1_000_000_000_000_000)
    buf[48] = 0
    price, mcap, grad = parse_curve_quote(bytes(buf))
    assert not grad
    assert 27.0 < mcap < 29.0
    assert price > 0
    buf[48] = 1
    _, _, grad = parse_curve_quote(bytes(buf))
    assert grad
    buf[48] = 0
    p6, m6, _ = parse_curve_quote(bytes(buf), 6)
    p9, m9, _ = parse_curve_quote(bytes(buf), 9)
    assert abs(m6 - m9) < 1e-9
    assert abs(p9 / p6 - 1000) < 1



def test_snipers_pct_not_count():
    assert verdict_from_risk(None) == (False, "snipers_unavailable")
    assert verdict_from_risk({"score": 10}) == (False, "snipers_unavailable")
    ok, reason = verdict_from_risk({"score": 10, "snipers": {"count": 61, "totalPercentage": 7.7}})
    assert ok and reason == "risk_10"
    ok, reason = verdict_from_risk({"score": 2, "snipers": {"count": 3, "totalPercentage": 25.0}})
    assert not ok and reason.startswith("snipers_25")
    assert verdict_from_risk({"score": 1, "rugged": True, "snipers": {"totalPercentage": 0}}) == (False, "rugged")


def test_amm_price_from_vault_reserves():
    # 1e15 raw tokens (1e9 UI @ 6dec), 50 SOL in quote vault
    parsed = amm_price_sol(1_000_000_000_000_000, 50_000_000_000)
    assert parsed is not None
    price, mcap = parsed
    assert abs(price - 5e-8) < 1e-12
    assert 49.0 < mcap < 51.0
    assert amm_price_sol(0, 1) is None


def test_parse_spl_amount():
    buf = bytearray(72)
    struct.pack_into("<Q", buf, 64, 123456789)
    assert parse_spl_amount(bytes(buf)) == 123456789
    assert parse_spl_amount(b"\x00" * 10) is None


def test_fdv_from_payload_matches_birth_curve():
    fdv, grad = _fdv_from_payload({
        "complete": False,
        "virtual_sol_reserves": 30_000_000_000,
        "virtual_token_reserves": 1_073_000_000_000_000,
        "total_supply": 1_000_000_000_000_000,
    })
    assert not grad
    assert 27.0 < fdv < 29.0


def test_fdv_from_payload_graduated():
    assert _fdv_from_payload({"complete": True, "virtual_sol_reserves": 1}) == (None, True)


def test_protocol_accounts_are_base58():
    mint = "63pXrV4infyMjiPAp3LF4qLC788vMLc3tmTXz3cSpump"
    accs = protocol_token_accounts(mint)
    assert len(accs) == 2
    assert all(len(a) >= 32 for a in accs)
