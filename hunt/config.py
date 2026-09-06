from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

HELIUS_HTTP = "https://mainnet.helius-rpc.com/?api-key={key}"
HELIUS_WS = "wss://mainnet.helius-rpc.com/?api-key={key}"
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
QUOTE_MINTS = {WSOL, USDC}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HUNT_", env_file=".env", extra="ignore")

    helius_api_key: str = ""
    birdeye_api_key: str = ""
    jup_api_key: str = ""
    gmgn_api_key: str = ""

    gmgn_prescreen_period: str = "30d"
    gmgn_min_recent_pnl_sol: float = -0.01
    gmgn_smartmoney_limit: int = 100
    gmgn_discovery_tokens_per_cycle: int = 15
    gmgn_traders_per_token: int = 30

    telegram_bot_token: str = ""
    telegram_chat_id: int = 0

    solanatracker_api_key: str = ""

    wallet_private_key: Optional[str] = None
    dry_run: bool = True

    data_dir: str = "hunt/data"

    scout_interval_s: int = 900
    scorer_interval_s: int = 300
    selector_interval_s: int = 3600
    stops_interval_s: int = 5
    daily_wallet_score_budget: int = 40
    rescore_after_h: int = 6
    birdeye_daily_cu_budget: int = 20000

    universe_days: int = 30
    universe_top_k_per_day: int = 20
    universe_min_day_volume_usd: float = 100000.0
    universe_min_day_gain_pct: float = 15.0
    universe_refresh_h: int = 24
    ohlcv_cache_ttl_h: int = 20
    ohlcv_workers: int = 4

    rpc_global_rps: float = 8.0
    rpc_global_burst: int = 16
    extraction_precise_top_tokens: int = 10
    precise_max_txs: int = 1500
    precise_max_sigs_pages: int = 6
    precise_top_wallets: int = 8

    graph_min_distinct_days: int = 2
    backtest_stage_days: int = 2
    backtest_parse_rate_gate: float = 0.25
    backtest_window_cap_txs: int = 400
    backtest_interval_h: int = 6
    backtest_days: int = 45

    max_tracked_wallets: int = 8
    max_open_positions: int = 100
    per_token_cap_sol: float = 0.3
    trade_size_sol: float = 0.05
    daily_loss_limit_sol: float = 0.5

    min_win_rate: float = 0.40
    min_trades: int = 30
    min_distinct_tokens: int = 5
    max_instant_sell_ratio: float = 0.15
    min_realized_pnl_sol: float = 1.0
    demote_inactivity_h: int = 72
    rolling_window_trades: int = 60

    take_profit_pct: float = 100.0
    stop_loss_pct: float = -30.0
    trailing_stop_pct: float = 20.0
    max_hold_hours: int = 48

    min_liquidity_usd: float = 15000.0
    min_market_cap_usd: float = 50000.0
    max_token_age_min: int = 0
    cooldown_s: int = 600
    min_whale_buy_sol: float = 0.05

    slippage_bps: int = 500
    priority_fee_max_lamports: int = 1_000_000
    priority_fee_percentile: int = 75
    jito_enabled: bool = False
    jito_tip_lamports: int = 100_000
    buy_retries: int = 3
    sell_retries: int = 5

    hot_token_min_liquidity_usd: float = 30000.0
    hot_token_min_vol24h_usd: float = 100000.0
    hot_token_min_change24h_pct: float = 40.0
    scout_max_new_tokens_per_run: int = 6
    replay_max_txs_per_wallet: int = 300

    log_level: str = "INFO"

    @computed_field
    @property
    def rpc_http(self) -> str:
        if self.helius_api_key:
            return HELIUS_HTTP.format(key=self.helius_api_key)
        return PUBLIC_RPC

    @computed_field
    @property
    def rpc_ws(self) -> str:
        return HELIUS_WS.format(key=self.helius_api_key)

    @computed_field
    @property
    def jup_base(self) -> str:
        return "https://quote-api.jup.ag/v6"


@lru_cache
def get_settings() -> Settings:
    return Settings()
