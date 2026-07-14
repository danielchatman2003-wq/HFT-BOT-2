"""Main entry point. Polls Kalshi's open BTC 15-minute markets, evaluates
each against the pricing model, and executes signals via the OrderManager.

Run modes (set MODE in .env):
  paper    - real market data, simulated fills, no orders ever sent. Default.
  live     - real orders on the configured Kalshi environment (demo or prod).
  backtest - see src/backtest.py instead; this file is for live/paper only.
"""
from __future__ import annotations

import logging
import time

from src.btc_price_feed import BTCPriceFeed
from src.config import CONFIG
from src.kalshi_client import KalshiClient
from src.order_manager import OrderManager
from src.risk_manager import RiskManager
from src.strategy import evaluate_market

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

POLL_INTERVAL_SECONDS = 5
MIN_VOL_SAMPLES_WARMUP_SECONDS = 120


def run(poll_interval: int = POLL_INTERVAL_SECONDS):
    if CONFIG.mode not in ("paper", "live"):
        raise SystemExit(f"MODE={CONFIG.mode!r} is not valid for bot.py; use 'paper' or 'live' (see src/backtest.py)")

    paper = CONFIG.mode == "paper"
    logger.info("Starting bot in %s mode (kalshi_env=%s)", CONFIG.mode, CONFIG.kalshi_env)

    price_feed = BTCPriceFeed()
    price_feed.start()

    client = KalshiClient()
    risk_manager = RiskManager()
    order_manager = OrderManager(risk_manager, kalshi_client=client, paper=paper)

    logger.info("Warming up price feed for %ds to build a volatility estimate...", MIN_VOL_SAMPLES_WARMUP_SECONDS)
    time.sleep(MIN_VOL_SAMPLES_WARMUP_SECONDS)

    try:
        while True:
            _tick(price_feed, client, order_manager)
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        logger.info("Shutting down.")
    finally:
        price_feed.stop()


def _tick(price_feed: BTCPriceFeed, client: KalshiClient, order_manager: OrderManager):
    spot = price_feed.latest_price()
    vol = price_feed.realized_vol_annualized(lookback_seconds=1800)
    if spot is None or vol is None:
        logger.info("Waiting on price feed warmup (spot=%s vol=%s)...", spot, vol)
        return

    try:
        markets = client.get_markets(series_ticker=CONFIG.btc_series_ticker, status="open")
    except Exception as exc:
        logger.warning("Failed to fetch markets: %s", exc)
        return

    for market in markets:
        signal = evaluate_market(market, spot=spot, annualized_vol=vol)
        if signal is None:
            continue
        try:
            order_manager.execute_signal(signal)
        except Exception:
            logger.exception("Failed to execute signal for %s", signal.ticker)


if __name__ == "__main__":
    run()
