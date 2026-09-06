from __future__ import annotations

from dataclasses import dataclass

VS0 = 30.0
VT0 = 1_073_000_000.0
K = VS0 * VT0
GRAD_REAL_SOL = 85.0


def curve_cum_inflow_at_completion(completion: float) -> float:
    return completion * GRAD_REAL_SOL


def marginal_price_sol_per_token(cum_inflow_sol: float) -> float:
    return (VS0 + cum_inflow_sol) ** 2 / K


def entry_price_ratio(entry_completion: float) -> float:
    x_in = curve_cum_inflow_at_completion(entry_completion)
    x_grad = curve_cum_inflow_at_completion(1.0)
    return marginal_price_sol_per_token(x_in) / marginal_price_sol_per_token(x_grad)


@dataclass
class SimConfig:
    entry_completion: float = 0.85
    slippage: float = 0.03
    fee_rate: float = 0.01
    take_profits: tuple[float, ...] = (0.40, 0.60, 0.80)
    stop_losses: tuple[float, ...] = (0.20, 0.30)
    trailing_pct: float = 0.15
    trailing_activation: float = 0.20
    max_hold_minutes: int = 360


def simulate_exit(
    candles: list[dict],
    entry_price_usd: float,
    cfg: SimConfig,
) -> dict:
    if not candles or entry_price_usd <= 0:
        return {"exit": "no_data", "return": 0.0}

    tp_levels = sorted(cfg.take_profits, reverse=True)
    sl_price = entry_price_usd * (1 - min(cfg.stop_losses))
    trail_stop_price: float | None = None
    peak = entry_price_usd

    for i, c in enumerate(candles):
        ts_min = (c["time"] - candles[0]["time"]) // 60
        hi, lo = c["high"], c["low"]

        if lo <= sl_price:
            return {
                "exit": "stop_loss", "return": sl_price / entry_price_usd - 1,
                "minutes": ts_min, "mfe": peak / entry_price_usd - 1,
            }
        if trail_stop_price is not None and lo <= trail_stop_price:
            return {
                "exit": "trailing", "return": trail_stop_price / entry_price_usd - 1,
                "minutes": ts_min, "mfe": peak / entry_price_usd - 1,
            }
        for tp in tp_levels:
            target = entry_price_usd * (1 + tp)
            if hi >= target:
                return {
                    "exit": f"take_profit_{int(tp*100)}",
                    "return": target / entry_price_usd - 1,
                    "minutes": ts_min, "mfe": peak / entry_price_usd - 1,
                }
        peak = max(peak, hi)
        if (
            trail_stop_price is None
            and peak >= entry_price_usd * (1 + cfg.trailing_activation)
        ):
            trail_stop_price = peak * (1 - cfg.trailing_pct)
        elif trail_stop_price is not None:
            trail_stop_price = max(trail_stop_price, peak * (1 - cfg.trailing_pct))

        if ts_min >= cfg.max_hold_minutes:
            return {
                "exit": "max_hold", "return": c["close"] / entry_price_usd - 1,
                "minutes": ts_min, "mfe": peak / entry_price_usd - 1,
            }

    last = candles[-1]
    return {
        "exit": "window_end", "return": last["close"] / entry_price_usd - 1,
        "minutes": (last["time"] - candles[0]["time"]) // 60,
        "mfe": peak / entry_price_usd - 1,
    }


def net_return(gross_return: float, cfg: SimConfig) -> float:
    cost_mult = (1 + cfg.slippage) * (1 + cfg.fee_rate)
    exit_fee = 1 - cfg.fee_rate
    return (1 + gross_return) / cost_mult * exit_fee - 1


@dataclass
class TieredConfig:
    tiers: tuple[tuple[float, float], ...] = (
        (0.03, 0.30), (0.05, 0.30), (0.08, 0.20), (0.20, 0.20),
    )
    trail_pct: float = 0.02
    hard_sl: float = -0.25
    max_hold_minutes: int = 360
    slippage: float = 0.03
    fee_rate: float = 0.01
    position_usd: float = 5.0


def simulate_tiered(
    candles: list[dict], entry_price_usd: float, cfg: TieredConfig
) -> dict:
    if not candles or entry_price_usd <= 0:
        return {"exit": "no_data", "net_usd": 0.0, "minutes": 0}

    pos = cfg.position_usd
    eff_entry = entry_price_usd * (1 + cfg.slippage)
    exit_mult = (1 - cfg.fee_rate)

    remaining = 1.0
    realized = 0.0
    peak = eff_entry
    trail_stop = eff_entry * (1 - cfg.trail_pct)
    filled_tiers = 0

    for c in candles:
        ts_min = (c["time"] - candles[0]["time"]) // 60
        hi, lo = c["high"], c["low"]

        sl_price = eff_entry * (1 + cfg.hard_sl)
        if lo <= sl_price and remaining > 0:
            realized += remaining * pos * ((sl_price / eff_entry)) * exit_mult
            return {
                "exit": "hard_sl", "net_usd": realized - pos,
                "minutes": ts_min,
                "tier_fills": filled_tiers,
            }

        if lo <= trail_stop and remaining > 0:
            realized += remaining * pos * (trail_stop / eff_entry) * exit_mult
            return {
                "exit": f"trail@{filled_tiers}", "net_usd": realized - pos,
                "minutes": ts_min, "tier_fills": filled_tiers,
            }

        for idx in range(filled_tiers, len(cfg.tiers)):
            gain, share = cfg.tiers[idx]
            target = eff_entry * (1 + gain)
            if hi >= target and remaining >= share - 1e-9:
                realized += share * pos * (target / eff_entry) * exit_mult
                remaining -= share
                filled_tiers = idx + 1
            else:
                break

        peak = max(peak, hi)
        trail_stop = max(trail_stop, peak * (1 - cfg.trail_pct))

        if ts_min >= cfg.max_hold_minutes and remaining > 0:
            realized += remaining * pos * (c["close"] / eff_entry) * exit_mult
            return {
                "exit": "max_hold", "net_usd": realized - pos,
                "minutes": ts_min, "tier_fills": filled_tiers,
            }

    last = candles[-1]
    if remaining > 0:
        realized += remaining * pos * (last["close"] / eff_entry) * exit_mult
    return {
        "exit": "window_end", "net_usd": realized - pos,
        "minutes": (last["time"] - candles[0]["time"]) // 60,
        "tier_fills": filled_tiers,
    }


def dead_trade_usd(cfg: TieredConfig) -> float:
    eff_entry = cfg.position_usd * (cfg.hard_sl * 0 + 1) * 0 + cfg.position_usd
    sl_exit_value = cfg.position_usd * (1 + cfg.hard_sl) * (1 - cfg.fee_rate)
    entry_cost = cfg.position_usd * (1 + cfg.fee_rate) * (1 + cfg.slippage) / (1 + cfg.fee_rate)
    _ = eff_entry, entry_cost
    return round(sl_exit_value - cfg.position_usd, 4)
