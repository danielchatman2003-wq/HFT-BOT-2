"""Kalshi market order book state and wire-format normalization.

Kalshi's book stores BIDS on both the YES and NO side; asks are implied
(an ask on YES at p is a bid on NO at 1-p). Everything here is normalized
into YES-dollar terms in [0, 1].

Wire formats: Kalshi is migrating from integer-cent payloads
(yes: [[96, 100], ...]) to dollar fixed-point strings
(yes_dollars_fp: [["0.9600", "100.00"], ...]). Both are accepted:
ints are cents; strings and floats <= 1.5 are dollars.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


def norm_price(v) -> float:
    """Normalize a Kalshi price value to YES-dollars."""
    if isinstance(v, bool):
        raise ValueError("bool is not a price")
    if isinstance(v, int):
        return v / 100.0
    if isinstance(v, str):
        return float(v)
    if isinstance(v, float):
        return v / 100.0 if v > 1.5 else v
    raise ValueError(f"unsupported price value: {v!r}")


def norm_qty(v) -> float:
    if isinstance(v, str):
        return float(v)
    return float(v)


@dataclass
class FillEvent:
    ticker: str
    order_id: str
    client_order_id: str
    signed_count: int          # + bought YES exposure, - sold YES exposure
    price: float               # YES-dollars paid/received per contract
    is_taker: bool
    ts: float


def parse_fill(msg: dict, now: float | None = None) -> FillEvent | None:
    """Normalize a fill message (ws `fill` channel) across API generations."""
    ticker = msg.get("market_ticker") or msg.get("ticker") or ""
    count_raw = msg.get("count") or msg.get("count_fp") or 0
    count = int(float(count_raw))
    if not ticker or count <= 0:
        return None

    side = (msg.get("side") or "").lower()
    action = (msg.get("action") or "").lower()
    if side in ("bid", "ask"):  # unified YES-book style
        sign = 1 if side == "bid" else -1
        price_raw = msg.get("price_dollars", msg.get("price", msg.get("yes_price")))
        price = norm_price(price_raw)
    else:  # legacy yes/no + buy/sell style
        sign = 1 if (action == "buy") == (side == "yes") else -1
        price_raw = msg.get("yes_price_dollars", msg.get("yes_price"))
        if price_raw is None:
            no_raw = msg.get("no_price_dollars", msg.get("no_price"))
            if no_raw is None:
                return None
            price = 1.0 - norm_price(no_raw)
        else:
            price = norm_price(price_raw)

    ts_raw = msg.get("ts_ms")
    if ts_raw is not None:
        ts = float(ts_raw) / 1000.0
    else:
        ts = float(msg.get("ts") or (now if now is not None else time.time()))
    return FillEvent(
        ticker=ticker,
        order_id=str(msg.get("order_id", "")),
        client_order_id=str(msg.get("client_order_id", "")),
        signed_count=sign * count,
        price=price,
        is_taker=bool(msg.get("is_taker", False)),
        ts=ts,
    )


class KalshiBook:
    """Order book for one market, in YES-dollar terms."""

    def __init__(self) -> None:
        self.yes_bids: dict[float, float] = {}
        self.no_bids: dict[float, float] = {}
        self.last_update: float = 0.0

    # ---- wire application ----

    def apply_snapshot(self, msg: dict, ts: float | None = None) -> None:
        self.yes_bids = self._parse_side(msg, "yes")
        self.no_bids = self._parse_side(msg, "no")
        self.last_update = ts if ts is not None else time.time()

    @staticmethod
    def _parse_side(msg: dict, side: str) -> dict[float, float]:
        arr = msg.get(f"{side}_dollars_fp")
        if arr is None:
            arr = msg.get(side) or []
        out: dict[float, float] = {}
        for price_raw, qty_raw in arr:
            q = norm_qty(qty_raw)
            if q > 0:
                out[norm_price(price_raw)] = q
        return out

    def apply_delta(self, msg: dict, ts: float | None = None) -> None:
        side = msg.get("side")
        price_raw = msg.get("price_dollars", msg.get("price"))
        delta_raw = msg.get("delta_fp", msg.get("delta"))
        if side not in ("yes", "no") or price_raw is None or delta_raw is None:
            return
        price = norm_price(price_raw)
        levels = self.yes_bids if side == "yes" else self.no_bids
        new_qty = levels.get(price, 0.0) + norm_qty(delta_raw)
        if new_qty > 1e-9:
            levels[price] = new_qty
        else:
            levels.pop(price, None)
        self.last_update = ts if ts is not None else time.time()

    # ---- YES-terms views ----

    def best_bid(self) -> float | None:
        return max(self.yes_bids) if self.yes_bids else None

    def best_ask(self) -> float | None:
        if not self.no_bids:
            return None
        return 1.0 - max(self.no_bids)

    def bbo(self) -> tuple[float | None, float | None]:
        return self.best_bid(), self.best_ask()

    def mid(self) -> float | None:
        bid, ask = self.bbo()
        if bid is None or ask is None:
            return None
        return 0.5 * (bid + ask)

    def ask_ladder(self) -> list[tuple[float, float]]:
        """Implied YES asks, ascending: (price, qty) from NO bids."""
        return sorted(((1.0 - p, q) for p, q in self.no_bids.items()), key=lambda x: x[0])

    def bid_ladder(self) -> list[tuple[float, float]]:
        """YES bids, descending."""
        return sorted(self.yes_bids.items(), key=lambda x: -x[0])

    def walk(self, side: str, count: int, limit_price: float | None = None) -> tuple[float, int]:
        """Simulate sweeping `count` contracts: side 'buy' walks the implied
        asks upward, 'sell' walks the bids downward, stopping at limit_price.
        Returns (avg_price, filled)."""
        ladder = self.ask_ladder() if side == "buy" else self.bid_ladder()
        remaining = count
        cash = 0.0
        filled = 0
        for price, qty in ladder:
            if limit_price is not None:
                if side == "buy" and price > limit_price + 1e-9:
                    break
                if side == "sell" and price < limit_price - 1e-9:
                    break
            take = min(remaining, int(qty))
            if take <= 0:
                continue
            cash += price * take
            filled += take
            remaining -= take
            if remaining <= 0:
                break
        return (cash / filled if filled else 0.0), filled

    def age_ms(self, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        if self.last_update == 0.0:
            return float("inf")
        return (now - self.last_update) * 1000.0
