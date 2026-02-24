from pydantic import Field
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    poly_api_key: str = Field(default="", env="POLY_API_KEY")
    poly_api_secret: str = Field(default="", env="POLY_API_SECRET")
    poly_api_passphrase: str = Field(default="", env="POLY_API_PASSPHRASE")
    poly_private_key: str = Field(default="", env="POLY_PRIVATE_KEY")

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

    env: str = Field("development", env="ENV")
    log_level: str = Field("INFO", env="LOG_LEVEL")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()
