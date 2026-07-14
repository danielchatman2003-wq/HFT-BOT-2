"""Kalshi trading-fee math.

Kalshi's published formula: fee = ceil_to_cent(rate * C * P * (1 - P)),
charged per executed order, where P is the contract price in dollars and C
the contract count. The general taker rate is 0.07, but crypto series carry
a higher multiplier and makers pay a reduced rate on designated series --
check kalshi.com/fee-schedule and set TAKER_FEE_RATE / MAKER_FEE_RATE to
match what's live. Fees are the single biggest reason small edges are not
tradable, so the strategy nets them out of every edge calculation.
"""
from __future__ import annotations

import math

_EPS = 1e-9


def order_fee(price: float, count: int, rate: float) -> float:
    """Total fee in dollars for an execution of `count` contracts at `price`
    (YES-dollars in [0, 1]), rounded UP to the next cent per Kalshi's rules."""
    if count <= 0:
        return 0.0
    p = min(max(price, 0.0), 1.0)
    raw = rate * count * p * (1.0 - p)
    return math.ceil(raw * 100.0 - _EPS) / 100.0


def fee_per_contract(price: float, rate: float, count: int = 1) -> float:
    """Effective per-contract fee for an execution of `count` contracts.

    Using count=1 (the default) gives the most conservative estimate, since
    the ceil-to-cent rounding weighs heaviest on single contracts.
    """
    if count <= 0:
        count = 1
    return order_fee(price, count, rate) / count
