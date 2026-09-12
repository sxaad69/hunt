from __future__ import annotations

BUNDLER_MAX = 0.50
RAT_MAX = 0.25
INSIDER_MAX = 0.25
RUG_MAX = 0.40
FRESH_MAX = 0.85


def _f(x) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _i(x) -> int | None:
    if x is None or x == "":
        return None
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def parse_token_flow(data: dict | None) -> dict:
    if not data or not isinstance(data, dict):
        return {}
    if "address" not in data and isinstance(data.get("data"), dict):
        data = data["data"]
    stat = data.get("stat") if isinstance(data.get("stat"), dict) else {}
    tags = data.get("wallet_tags_stat") if isinstance(data.get("wallet_tags_stat"), dict) else {}
    return {
        "address": data.get("address") or "",
        "symbol": data.get("symbol") or "",
        "bundler": _f(stat.get("top_bundler_trader_percentage") or data.get("bundler_trader_amount_rate")),
        "rat": _f(stat.get("top_rat_trader_percentage") or data.get("rat_trader_amount_rate")),
        "insider": _f(data.get("suspected_insider_hold_rate") or stat.get("suspected_insider_hold_rate")),
        "fresh": _f(stat.get("fresh_wallet_rate") or data.get("fresh_wallet_rate")),
        "rug": _f(data.get("rug_ratio") or stat.get("rug_ratio")),
        "smart_degen": _i(tags.get("smart_wallets") or data.get("smart_degen_count")),
        "renowned": _i(tags.get("renowned_wallets") or data.get("renowned_count")),
        "sniper_hold": _f(stat.get("top70_sniper_hold_rate") or data.get("top70_sniper_hold_rate")),
        "created_ts": _i(
            data.get("creation_timestamp") or data.get("created_timestamp") or data.get("open_timestamp")
        ),
    }


def flow_verdict(flow: dict | None) -> tuple[bool, str]:
    if not flow:
        return True, ""
    b = flow.get("bundler")
    if b is not None and b > BUNDLER_MAX:
        return False, f"gmgn_bundler_{int(b * 100)}"
    r = flow.get("rat")
    if r is not None and r > RAT_MAX:
        return False, f"gmgn_rat_{int(r * 100)}"
    ins = flow.get("insider")
    if ins is not None and ins > INSIDER_MAX:
        return False, f"gmgn_insider_{int(ins * 100)}"
    rug = flow.get("rug")
    if rug is not None and rug > RUG_MAX:
        return False, f"gmgn_rug_{int(rug * 100)}"
    fresh = flow.get("fresh")
    if fresh is not None and fresh > FRESH_MAX:
        return False, f"gmgn_fresh_{int(fresh * 100)}"
    bits = []
    sd = flow.get("smart_degen")
    if sd:
        bits.append(f"sm{sd}")
    kol = flow.get("renowned")
    if kol:
        bits.append(f"kol{kol}")
    return True, ("+" + "+".join(bits)) if bits else ""
