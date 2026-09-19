"""Application settings, loaded from environment variables / .env."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="OPTIONS_AGENT_")

    # Data source: "mock" runs entirely offline with synthetic data so the
    # dashboard can be exercised without a broker connection. "ibkr" connects
    # to a running TWS / IB Gateway instance via ib_insync.
    data_provider: str = "mock"

    # IBKR TWS / IB Gateway connection (only used when data_provider == "ibkr")
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497  # 7497 = TWS paper, 7496 = TWS live, 4002 = Gateway paper, 4001 = Gateway live
    ibkr_client_id: int = 7

    # Default symbols shown on the dashboard watchlist
    watchlist: list[str] = ["AAPL", "MSFT", "SPY", "NVDA", "TSLA"]

    risk_free_rate: float = 0.05

    # Strategy screener defaults
    min_days_to_expiry: int = 7
    max_days_to_expiry: int = 60

    # Unusual activity thresholds
    unusual_volume_zscore: float = 2.5
    unusual_volume_oi_ratio: float = 1.0  # volume >= this multiple of open interest


settings = Settings()
