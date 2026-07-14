"""Backtest the strategy against historical BTC price data.

IMPORTANT LIMITATION -- read before trusting any number this produces:

Kalshi does not publish a free historical feed of past order-book prices for
settled 15-minute BTC markets. Without real historical market prices, we
cannot replay "what would the market have offered, and would our model have
disagreed enough to trade." This script instead simulates a *synthetic*
market maker that prices each contract using the SAME pricing model the
strategy uses, plus injected noise/spread. That means:

  - It can validate that the strategy's plumbing (sizing, risk limits,
    settlement) works correctly.
  - It CANNOT tell you whether the model has a real edge over Kalshi's actual
    market makers, because the "market" here is not real. A strategy that
    looks profitable against its own model, priced with its own model, is
    close to circular by construction.

To actually validate edge, you need one of:
  1. Live paper trading against real Kalshi market prices (MODE=paper) for
     an extended period, comparing model probability vs. actual quotes.
  2. Kalshi's historical trades/candlestick endpoints (available to
     authenticated accounts for some markets) fed in as `market_price_cents`
     instead of the synthetic quote below.

Input: a CSV with columns `timestamp` (unix seconds) and `price` (BTC-USD),
at roughly 1-second or better resolution. Produces 15-minute windows,
computes realized vol trailing each window, and reports simulated PnL.
"""
from __future__ import annotations

import argparse
import math
import random
from dataclasses import dataclass

import pandas as pd

from src.pricing_model import fair_value_above_strike
from src.risk_manager import RiskManager

WINDOW_SECONDS = 15 * 60
VOL_LOOKBACK_SECONDS = 30 * 60
SYNTHETIC_SPREAD_CENTS = 2.0  # market maker spread baked into the synthetic quote
SYNTHETIC_NOISE_STD_CENTS = 3.0  # random mispricing noise around the "true" model price


@dataclass
class Trade:
    window_start: float
    side: str
    contracts: int
    entry_price_cents: float
    resolved_yes: bool
    pnl_usd: float


def _annualized_realized_vol(returns_per_sqrt_sec: list[float]) -> float | None:
    if len(returns_per_sqrt_sec) < 5:
        return None
    mean = sum(returns_per_sqrt_sec) / len(returns_per_sqrt_sec)
    var = sum((r - mean) ** 2 for r in returns_per_sqrt_sec) / (len(returns_per_sqrt_sec) - 1)
    seconds_per_year = 365 * 24 * 3600
    return math.sqrt(var) * math.sqrt(seconds_per_year)


def _synthetic_market_quote(true_prob_yes: float, rng: random.Random) -> tuple[float, float]:
    """Fabricates yes_ask/no_ask around the model's own probability, with
    spread and noise, purely so the sizing/risk/settlement logic has
    something to trade against. See module docstring for why this cannot
    validate real edge."""
    true_price = true_prob_yes * 100
    noisy_mid = max(1.0, min(99.0, true_price + rng.gauss(0, SYNTHETIC_NOISE_STD_CENTS)))
    yes_ask = min(99.0, noisy_mid + SYNTHETIC_SPREAD_CENTS / 2)
    no_ask = min(99.0, (100 - noisy_mid) + SYNTHETIC_SPREAD_CENTS / 2)
    return yes_ask, no_ask


def run_backtest(csv_path: str, min_edge_cents: float = 4.0, seed: int = 42) -> list[Trade]:
    df = pd.read_csv(csv_path).sort_values("timestamp")
    rng = random.Random(seed)
    risk_manager = RiskManager()
    trades: list[Trade] = []

    t_start = df["timestamp"].iloc[0]
    t_end = df["timestamp"].iloc[-1]

    window_start = t_start
    while window_start + WINDOW_SECONDS <= t_end:
        window_end = window_start + WINDOW_SECONDS
        vol_window = df[(df.timestamp >= window_start - VOL_LOOKBACK_SECONDS) & (df.timestamp <= window_start)]
        if len(vol_window) < 10:
            window_start = window_end
            continue

        prices = vol_window["price"].to_numpy()
        times = vol_window["timestamp"].to_numpy()
        returns = []
        for i in range(1, len(prices)):
            dt = times[i] - times[i - 1]
            if dt <= 0 or prices[i - 1] <= 0 or prices[i] <= 0:
                continue
            returns.append(math.log(prices[i] / prices[i - 1]) / math.sqrt(dt))
        vol = _annualized_realized_vol(returns)

        window_df = df[(df.timestamp >= window_start) & (df.timestamp <= window_end)]
        if vol is None or window_df.empty:
            window_start = window_end
            continue

        spot_open = window_df["price"].iloc[0]
        spot_close = window_df["price"].iloc[-1]
        strike = spot_open  # Kalshi's 15-min markets commonly strike at-the-money at window open

        fv = fair_value_above_strike(spot_open, strike, WINDOW_SECONDS, vol)
        yes_ask, no_ask = _synthetic_market_quote(fv.prob_yes, rng)

        yes_edge = fv.yes_price_cents - yes_ask
        no_edge = fv.no_price_cents - no_ask

        side, model_prob, price_cents = None, None, None
        if yes_edge >= min_edge_cents and yes_edge >= no_edge:
            side, model_prob, price_cents = "yes", fv.prob_yes, yes_ask
        elif no_edge >= min_edge_cents:
            side, model_prob, price_cents = "no", fv.prob_no, no_ask

        if side is not None:
            sizing = risk_manager.size_position(model_prob, price_cents)
            contracts = sizing["contracts"]
            if contracts > 0:
                resolved_yes = spot_close > strike
                won = (side == "yes") == resolved_yes
                pnl_usd = ((100.0 if won else 0.0) - price_cents) / 100.0 * contracts
                risk_manager.record_position_opened()
                risk_manager.record_position_closed(pnl_usd)
                trades.append(Trade(window_start, side, contracts, price_cents, resolved_yes, pnl_usd))

        window_start = window_end

    return trades


def summarize(trades: list[Trade]):
    if not trades:
        print("No trades generated.")
        return
    total_pnl = sum(t.pnl_usd for t in trades)
    wins = sum(1 for t in trades if t.pnl_usd > 0)
    print(f"Trades: {len(trades)}  Wins: {wins} ({wins/len(trades)*100:.1f}%)")
    print(f"Total simulated PnL: ${total_pnl:.2f}")
    print("\nNOTE: this PnL is against a SYNTHETIC market priced from the same")
    print("model used to trade -- it demonstrates plumbing correctness, not real edge.")
    print("See the module docstring for how to validate against real Kalshi prices.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", help="CSV with columns: timestamp,price")
    parser.add_argument("--min-edge-cents", type=float, default=4.0)
    args = parser.parse_args()

    results = run_backtest(args.csv_path, min_edge_cents=args.min_edge_cents)
    summarize(results)
