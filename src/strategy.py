"""Edge-detection strategy: compare the pricing model's fair probability to
the current Kalshi market price and signal a trade only when the edge is
large enough to plausibly survive fees, slippage, and model error.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from src.config import CONFIG
from src.pricing_model import fair_value_above_strike


@dataclass
class Signal:
    ticker: str
    side: str  # "yes" or "no"
    model_prob: float
    market_price_cents: float
    edge_cents: float


def parse_strike_from_market(market: dict) -> float | None:
    """Kalshi markets expose the strike via `cap_strike`/`floor_strike` for
    range markets, or embed it in the subtitle/ticker for threshold markets.
    Try the structured fields first, then fall back to parsing the title.
    """
    for key in ("cap_strike", "floor_strike", "strike_price"):
        val = market.get(key)
        if val:
            return float(val)

    title = market.get("title", "") or market.get("subtitle", "")
    match = re.search(r"\$?([\d,]+(?:\.\d+)?)", title)
    if match:
        return float(match.group(1).replace(",", ""))
    return None


def seconds_to_expiry(market: dict) -> float | None:
    close_time = market.get("close_time") or market.get("expiration_time")
    if not close_time:
        return None
    try:
        expiry = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (expiry - datetime.now(timezone.utc)).total_seconds()


def evaluate_market(
    market: dict,
    spot: float,
    annualized_vol: float,
    min_edge_cents: float = CONFIG.min_edge_cents,
) -> Signal | None:
    """Returns a trade Signal if the model disagrees enough with the market,
    else None. `market` is a Kalshi market dict from get_markets()/get_market().
    """
    strike = parse_strike_from_market(market)
    ttl = seconds_to_expiry(market)
    if strike is None or ttl is None or ttl <= 0:
        return None

    fv = fair_value_above_strike(spot, strike, ttl, annualized_vol)

    yes_ask = market.get("yes_ask")
    no_ask = market.get("no_ask")
    if yes_ask is None or no_ask is None:
        return None

    yes_edge = fv.yes_price_cents - yes_ask
    no_edge = fv.no_price_cents - no_ask

    if yes_edge >= min_edge_cents and yes_edge >= no_edge:
        return Signal(
            ticker=market["ticker"],
            side="yes",
            model_prob=fv.prob_yes,
            market_price_cents=yes_ask,
            edge_cents=yes_edge,
        )
    if no_edge >= min_edge_cents:
        return Signal(
            ticker=market["ticker"],
            side="no",
            model_prob=fv.prob_no,
            market_price_cents=no_ask,
            edge_cents=no_edge,
        )
    return None
