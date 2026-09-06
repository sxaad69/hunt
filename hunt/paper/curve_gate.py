from __future__ import annotations
from dataclasses import dataclass

VS0 = 30.0
VT0 = 1_073_000_000.0
K = VS0 * VT0

@dataclass
class CurveGateConfig:
    min_curve_liquidity_sol: float = 30.0
    max_price_impact_pct: float = 5.0
    max_round_trip_cost_pct: float = 8.0
    fee_pct: float = 1.0
    slippage_pct: float = 3.0

def curve_liquidity_sol(virtual_sol: float) -> float:
    return virtual_sol

def price_impact_pct(size_sol: float, virtual_sol: float, fee_pct: float = 1.0) -> float:
    if virtual_sol <= 0: return 100.0
    f = fee_pct / 100.0
    # (1+s/S)/(1-f) -1
    return ((1 + size_sol / virtual_sol) / (1 - f) - 1) * 100

def round_trip_cost_pct(virtual_sol: float, size_sol: float, fee_pct: float = 1.0, slip_pct: float = 3.0) -> float:
    # entry + exit cost approx 2*(fee+slip+impact)
    impact = price_impact_pct(size_sol, virtual_sol, fee_pct)
    return 2 * (fee_pct + slip_pct) + impact

def check_curve(virtual_sol: float, size_sol: float, cfg: CurveGateConfig = CurveGateConfig()) -> tuple[bool, str]:
    if virtual_sol < cfg.min_curve_liquidity_sol:
        return False, f"thin_curve_{virtual_sol:.1f}SOL"
    impact = price_impact_pct(size_sol, virtual_sol, cfg.fee_pct)
    if impact > cfg.max_price_impact_pct:
        return False, f"high_impact_{impact:.1f}%"
    rt = round_trip_cost_pct(virtual_sol, size_sol, cfg.fee_pct, cfg.slippage_pct)
    if rt > cfg.max_round_trip_cost_pct:
        return False, f"high_round_trip_{rt:.1f}%"
    return True, "pass"
