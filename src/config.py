"""Central configuration loaded from environment variables (.env).

Everything tunable lives here so strategy/risk behavior is auditable in one
place. Prices are handled internally in YES-dollars (0.0 - 1.0); config
fields expressed in cents are converted at the edges for readability.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _b(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(x.strip().lower() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Config:
    # ---- run mode ----
    mode: str = os.getenv("MODE", "paper")  # paper | live

    # ---- Kalshi credentials / environment ----
    kalshi_api_key_id: str = os.getenv("KALSHI_API_KEY_ID", "")
    kalshi_private_key_path: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi_private_key.pem")
    kalshi_env: str = os.getenv("KALSHI_ENV", "demo")  # demo | prod
    kalshi_base_url_override: str = os.getenv("KALSHI_BASE_URL", "")
    kalshi_ws_url_override: str = os.getenv("KALSHI_WS_URL", "")
    # Kalshi is migrating order placement to a unified YES-book API
    # (side=bid/ask, decimal dollar prices, /portfolio/events/orders).
    # "v2" uses that; "legacy" uses the older /portfolio/orders
    # (side=yes/no + action + integer cents). If order placement 4xx's on
    # one flavor, try the other -- and verify against docs.kalshi.com,
    # which was not reachable from the environment this was written in.
    kalshi_order_api: str = os.getenv("KALSHI_ORDER_API", "v2")  # v2 | legacy

    # Kalshi is migrating no-side orderbook prices to yes-leg pricing
    # (`use_yes_price` subscribe flag; default currently false, will flip).
    # When true, we request and parse yes-leg pricing on the no side.
    kalshi_use_yes_price: bool = _b("KALSHI_USE_YES_PRICE", False)

    # ---- market selection ----
    series_ticker: str = os.getenv("SERIES_TICKER", "KXBTC15M")
    market_poll_seconds: float = _f("MARKET_POLL_SECONDS", 20.0)

    # ---- rate limits (tokens/sec; Kalshi basic tier: 200 read, 100 write,
    #      default cost 10 tokens/request => ~20 reads/s, ~10 writes/s) ----
    read_tokens_per_sec: float = _f("READ_TOKENS_PER_SEC", 200.0)
    write_tokens_per_sec: float = _f("WRITE_TOKENS_PER_SEC", 100.0)
    tokens_per_request: float = _f("TOKENS_PER_REQUEST", 10.0)

    # ---- fees (see kalshi.com/fee-schedule; crypto series carry a HIGHER
    #      multiplier than the general 0.07 -- verify the live schedule and
    #      set these to match; overestimating is the safe direction) ----
    taker_fee_rate: float = _f("TAKER_FEE_RATE", 0.10)
    maker_fee_rate: float = _f("MAKER_FEE_RATE", 0.025)

    # ---- BRTI source ----
    # auto     -> official Kalshi-streamed CF value when fresh, replica fallback
    # official -> only the cfbenchmarks_value websocket feed
    # replica  -> only the local constituent-book estimator
    brti_source: str = os.getenv("BRTI_SOURCE", "auto")
    brti_index_id: str = os.getenv("BRTI_INDEX_ID", "BRTI")  # ETH: ETHUSD_RTI

    # ---- BRTI replica estimator ----
    brti_feeds: tuple[str, ...] = field(default_factory=lambda: _list("BRTI_FEEDS", "coinbase,kraken,bitstamp,gemini"))
    brti_lambda: float = _f("BRTI_LAMBDA", 10.3)          # exp-weight decay (CF methodology)
    brti_deviation_cap: float = _f("BRTI_DEVIATION_CAP", 0.005)  # utilized-depth half-spread cap (0.5%)
    brti_min_depth_btc: float = _f("BRTI_MIN_DEPTH_BTC", 1.0)
    brti_feed_stale_ms: float = _f("BRTI_FEED_STALE_MS", 5000.0)   # drop a feed's book if silent this long
    brti_stale_ms: float = _f("BRTI_STALE_MS", 2500.0)             # halt trading if index older than this
    settlement_window_s: float = _f("SETTLEMENT_WINDOW_S", 60.0)   # Kalshi: mean of final 60 1-sec BRTI prints
    settlement_ticks: int = _i("SETTLEMENT_TICKS", 60)

    # ---- volatility estimator ----
    vol_half_life_s: float = _f("VOL_HALF_LIFE_S", 300.0)
    vol_min_samples: int = _i("VOL_MIN_SAMPLES", 45)
    vol_floor_annual: float = _f("VOL_FLOOR_ANNUAL", 0.15)
    vol_cap_annual: float = _f("VOL_CAP_ANNUAL", 4.0)
    vol_winsor_k: float = _f("VOL_WINSOR_K", 8.0)

    # ---- strategy ----
    enable_taker: bool = _b("ENABLE_TAKER", True)
    enable_maker: bool = _b("ENABLE_MAKER", True)
    min_taker_edge_cents: float = _f("MIN_TAKER_EDGE_CENTS", 3.0)   # net of taker fee
    min_maker_edge_cents: float = _f("MIN_MAKER_EDGE_CENTS", 1.0)   # net of maker fee
    maker_vol_mult: float = _f("MAKER_VOL_MULT", 1.6)               # half-spread = mult * repricing-horizon prob vol
    maker_horizon_s: float = _f("MAKER_HORIZON_S", 3.0)
    reprice_threshold_cents: float = _f("REPRICE_THRESHOLD_CENTS", 1.0)
    min_replace_interval_ms: float = _f("MIN_REPLACE_INTERVAL_MS", 400.0)
    quote_ttl_s: float = _f("QUOTE_TTL_S", 10.0)          # server-side safety expiry on resting quotes
    quote_size: int = _i("QUOTE_SIZE", 10)                # contracts per quote
    taker_cooldown_s: float = _f("TAKER_COOLDOWN_S", 1.0)
    maker_stop_before_close_s: float = _f("MAKER_STOP_BEFORE_CLOSE_S", 12.0)
    taker_stop_before_close_s: float = _f("TAKER_STOP_BEFORE_CLOSE_S", 1.5)
    extreme_prob_no_quote: float = _f("EXTREME_PROB_NO_QUOTE", 0.985)  # stop quoting when fair beyond this
    price_tick: float = _f("KALSHI_PRICE_TICK", 0.01)     # dollars

    # ---- risk ----
    bankroll_usd: float = _f("BANKROLL_USD", 1000.0)
    max_pos_per_market: int = _i("MAX_POS_PER_MARKET", 50)      # cap on |signed YES contracts|
    max_taker_size: int = _i("MAX_TAKER_SIZE", 20)
    max_total_notional_usd: float = _f("MAX_TOTAL_NOTIONAL_USD", 500.0)
    max_risk_per_trade: float = _f("MAX_RISK_PER_TRADE", 0.02)  # fraction of bankroll
    kelly_fraction: float = _f("KELLY_FRACTION", 0.25)
    daily_loss_limit_usd: float = _f("DAILY_LOSS_LIMIT_USD", 100.0)
    max_consecutive_order_errors: int = _i("MAX_CONSECUTIVE_ORDER_ERRORS", 5)
    kalshi_ws_stale_ms: float = _f("KALSHI_WS_STALE_MS", 5000.0)

    # ---- logging ----
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    status_interval_s: float = _f("STATUS_INTERVAL_S", 5.0)
    trade_log_path: str = os.getenv("TRADE_LOG_PATH", "logs/trades.csv")

    @property
    def kalshi_host(self) -> str:
        if self.kalshi_base_url_override:
            return self.kalshi_base_url_override.rstrip("/")
        if self.kalshi_env == "prod":
            return "https://api.elections.kalshi.com"
        return "https://demo-api.kalshi.co"

    @property
    def kalshi_ws_host(self) -> str:
        if self.kalshi_ws_url_override:
            return self.kalshi_ws_url_override.rstrip("/")
        if self.kalshi_env == "prod":
            # Per Kalshi's AsyncAPI spec: the production websocket lives on a
            # dedicated host, not the REST host.
            return "wss://external-api-ws.kalshi.com"
        return "wss://demo-api.kalshi.co"

    @property
    def min_taker_edge(self) -> float:
        return self.min_taker_edge_cents / 100.0

    @property
    def min_maker_edge(self) -> float:
        return self.min_maker_edge_cents / 100.0

    @property
    def reprice_threshold(self) -> float:
        return self.reprice_threshold_cents / 100.0


CONFIG = Config()
