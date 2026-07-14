"""Central configuration loaded from environment variables (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass(frozen=True)
class Config:
    mode: str = os.getenv("MODE", "paper")

    kalshi_api_key_id: str = os.getenv("KALSHI_API_KEY_ID", "")
    kalshi_private_key_path: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi_private_key.pem")
    kalshi_env: str = os.getenv("KALSHI_ENV", "demo")

    bankroll_usd: float = _f("BANKROLL_USD", 1000)
    max_risk_per_trade: float = _f("MAX_RISK_PER_TRADE", 0.02)
    max_concurrent_positions: int = _i("MAX_CONCURRENT_POSITIONS", 3)
    daily_loss_limit_usd: float = _f("DAILY_LOSS_LIMIT_USD", 100)
    kelly_fraction: float = _f("KELLY_FRACTION", 0.25)
    min_edge_cents: float = _f("MIN_EDGE_CENTS", 4)

    btc_series_ticker: str = os.getenv("BTC_SERIES_TICKER", "KXBTCD")
    coinbase_ws_url: str = os.getenv("COINBASE_WS_URL", "wss://ws-feed.exchange.coinbase.com")

    @property
    def kalshi_base_url(self) -> str:
        if self.kalshi_env == "prod":
            return "https://trading-api.kalshi.com/trade-api/v2"
        return "https://demo-api.kalshi.co/trade-api/v2"


CONFIG = Config()
