"""Minimal L2 order-book state for a single exchange's BTC-USD market."""
from __future__ import annotations

import time


class L2Book:
    """Price-level book. Prices/quantities are floats; qty <= 0 removes a level."""

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_update: float = 0.0

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()

    def replace(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]], ts: float | None = None) -> None:
        self.bids = {p: q for p, q in bids if q > 0.0 and p > 0.0}
        self.asks = {p: q for p, q in asks if q > 0.0 and p > 0.0}
        self.last_update = ts if ts is not None else time.time()

    def set_level(self, side: str, price: float, qty: float, ts: float | None = None) -> None:
        if price <= 0.0:
            return
        levels = self.bids if side == "bid" else self.asks
        if qty > 0.0:
            levels[price] = qty
        else:
            levels.pop(price, None)
        self.last_update = ts if ts is not None else time.time()

    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    @property
    def crossed(self) -> bool:
        bb, ba = self.best_bid(), self.best_ask()
        return bb is not None and ba is not None and bb >= ba

    @property
    def two_sided(self) -> bool:
        return bool(self.bids) and bool(self.asks)

    def age_ms(self, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        return (now - self.last_update) * 1000.0
