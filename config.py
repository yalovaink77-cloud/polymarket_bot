from pydantic import Field
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    poly_api_key: str = Field(default="", env="POLY_API_KEY")
    poly_api_secret: str = Field(default="", env="POLY_API_SECRET")
    poly_api_passphrase: str = Field(default="", env="POLY_API_PASSPHRASE")
    poly_private_key: str = Field(default="", env="POLY_PRIVATE_KEY")

    # Hava durumu sağlayıcısı için API anahtarı
    weather_api_key: str = Field(default="", env="WEATHER_API_KEY")
    weather_poll_interval: int = Field(60, env="WEATHER_POLL_INTERVAL")

    telegram_bot_token: str = Field(default="", env="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", env="TELEGRAM_CHAT_ID")

    max_position_pct: float = Field(0.07, env="MAX_POSITION_PCT")
    max_correlated_pct: float = Field(0.25, env="MAX_CORRELATED_PCT")
    capital_total_usd: float = Field(1000.0, env="CAPITAL_TOTAL_USD")

    imbalance_high: float = Field(1.8, env="IMBALANCE_HIGH")
    imbalance_low: float = Field(0.55, env="IMBALANCE_LOW")
    depth_ratio_min: float = Field(0.4, env="DEPTH_RATIO_MIN")
    zscore_threshold: float = Field(2.7, env="ZSCORE_THRESHOLD")
    overreaction_threshold: float = Field(3.0, env="OVERREACTION_THRESHOLD")
    spread_zscore_threshold: float = Field(2.5, env="SPREAD_ZSCORE_THRESHOLD")
    top3_concentration_min: float = Field(0.70, env="TOP3_CONCENTRATION_MIN")
    composite_score_min: float = Field(0.75, env="COMPOSITE_SCORE_MIN")

    # Near-resolution filtresi: mid price bu aralık dışındaysa sinyal yoksay
    near_resolution_max: float = Field(0.88, env="NEAR_RESOLUTION_MAX")
    near_resolution_min: float = Field(0.12, env="NEAR_RESOLUTION_MIN")

    # Momentum vs mean-reversion ayrımı için overreaction eşiği
    momentum_overreaction_min: float = Field(0.65, env="MOMENTUM_OVERREACTION_MIN")

    # Aynı market için sinyal cooldown süresi (saniye)
    signal_cooldown_sec: float = Field(300.0, env="SIGNAL_COOLDOWN_SEC")

    # Onay beklemeden sinyalleri otomatik uygula
    auto_execute: bool = Field(True, env="AUTO_EXECUTE")

    env: str = Field("development", env="ENV")
    require_telegram_approval: bool = Field(False, env="REQUIRE_TELEGRAM_APPROVAL")
    auto_switch_to_live: bool = Field(False, env="AUTO_SWITCH_TO_LIVE")
    dry_run_eval_horizon_sec: int = Field(300, env="DRY_RUN_EVAL_HORIZON_SEC")
    dry_run_min_trades: int = Field(20, env="DRY_RUN_MIN_TRADES")
    dry_run_min_win_rate: float = Field(0.55, env="DRY_RUN_MIN_WIN_RATE")
    dry_run_min_net_pnl_usd: float = Field(0.0, env="DRY_RUN_MIN_NET_PNL_USD")
    dry_run_pnl_floor_price: float = Field(0.05, env="DRY_RUN_PNL_FLOOR_PRICE")
    dry_run_state_file: str = Field("logs/dry_run_stats.json", env="DRY_RUN_STATE_FILE")
    dry_run_stop_loss_pct: float = Field(0.10, env="DRY_RUN_STOP_LOSS_PCT")
    dry_run_take_profit_pct: float = Field(0.08, env="DRY_RUN_TAKE_PROFIT_PCT")
    log_level: str = Field("INFO", env="LOG_LEVEL")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()
