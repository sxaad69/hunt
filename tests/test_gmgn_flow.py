from hunt.gmgn.client import unwrap_payload
from hunt.gmgn.flow import flow_verdict, parse_token_flow


def test_unwrap_envelope():
    assert unwrap_payload({"code": 0, "data": {"rank": [1]}}) == {"rank": [1]}
    assert unwrap_payload({"list": []}) == {"list": []}
    assert unwrap_payload({"code": 401, "data": {}, "message": "no"}) is None


def test_parse_token_info_stat():
    flow = parse_token_flow({
        "address": "AbcPump",
        "symbol": "X",
        "stat": {
            "top_bundler_trader_percentage": "0.12",
            "top_rat_trader_percentage": "0.01",
            "fresh_wallet_rate": "0.20",
            "top70_sniper_hold_rate": "0.05",
        },
        "wallet_tags_stat": {"smart_wallets": 4, "renowned_wallets": 2},
        "suspected_insider_hold_rate": 0.0,
        "rug_ratio": 0.02,
    })
    assert flow["bundler"] == 0.12
    assert flow["rat"] == 0.01
    assert flow["smart_degen"] == 4
    assert flow["renowned"] == 2
    ok, reason = flow_verdict(flow)
    assert ok
    assert "sm4" in reason and "kol2" in reason


def test_parse_trenches_row():
    flow = parse_token_flow({
        "address": "Mintpump",
        "symbol": "BATON",
        "bundler_trader_amount_rate": 0.39,
        "rat_trader_amount_rate": 0.0,
        "suspected_insider_hold_rate": 0.0,
        "fresh_wallet_rate": 0.01,
        "rug_ratio": 0.02,
        "smart_degen_count": 1,
        "renowned_count": 1,
    })
    assert flow["bundler"] == 0.39
    ok, reason = flow_verdict(flow)
    assert ok and "sm1" in reason


def test_flow_rejects():
    assert flow_verdict(None) == (True, "")
    assert flow_verdict({}) == (True, "")
    ok, reason = flow_verdict({"bundler": 0.67})
    assert not ok and reason.startswith("gmgn_bundler_")
    ok, reason = flow_verdict({"rat": 0.40})
    assert not ok and reason.startswith("gmgn_rat_")
    ok, reason = flow_verdict({"insider": 0.40})
    assert not ok and reason.startswith("gmgn_insider_")
    ok, reason = flow_verdict({"rug": 0.55})
    assert not ok and reason.startswith("gmgn_rug_")
    ok, reason = flow_verdict({"fresh": 0.91})
    assert not ok and reason.startswith("gmgn_fresh_")
    ok, reason = flow_verdict({"bundler": 0.49, "rat": 0.24})
    assert ok
