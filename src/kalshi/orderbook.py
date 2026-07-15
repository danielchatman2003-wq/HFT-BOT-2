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
    fee: float | None = None            # actual exchange fee (fill channel's fee_cost)
    post_position: float | None = None  # exchange-reported net position after the fill


def parse_fill(msg: dict, now: float | None = None) -> FillEvent | None:
    """Normalize a fill message (ws `fill` channel).

    Direction resolution follows Kalshi's migration order: the canonical
    `book_side` ('bid'/'ask') / `outcome_side` ('yes'/'no') fields first
    (buy-yes and sell-no both produce 'yes' = +YES exposure), then the
    deprecated `side`+`action` pair as fallback."""
    ticker = msg.get("market_ticker") or msg.get("ticker") or ""
    count_raw = msg.get("count_fp", msg.get("count", 0))
    try:
        count = int(float(count_raw))
    except (TypeError, ValueError):
        return None
    if not ticker or count <= 0:
        return None

    book_side = (msg.get("book_side") or "").lower()
    outcome_side = (msg.get("outcome_side") or "").lower()
    side = (msg.get("side") or "").lower()
    action = (msg.get("action") or "").lower()
    if book_side in ("bid", "ask"):
        sign = 1 if book_side == "bid" else -1
    elif outcome_side in ("yes", "no"):
        sign = 1 if outcome_side == "yes" else -1
    elif side in ("bid", "ask"):
        sign = 1 if side == "bid" else -1
    elif side in ("yes", "no") and action in ("buy", "sell"):
        sign = 1 if (action == "buy") == (side == "yes") else -1
    else:
        return None

    price_raw = msg.get("yes_price_dollars", msg.get("yes_price"))
    if price_raw is None:
        price_raw = msg.get("price_dollars", msg.get("price"))
    if price_raw is not None:
        price = norm_price(price_raw)
    else:
        no_raw = msg.get("no_price_dollars", msg.get("no_price"))
        if no_raw is None:
            return None
        price = 1.0 - norm_price(no_raw)

    fee_raw = msg.get("fee_cost")
    fee = None
    if fee_raw is not None:
        try:
            fee = float(fee_raw)
        except (TypeError, ValueError):
            fee = None
    post_raw = msg.get("post_position_fp")
    post_position = None
    if post_raw is not None:
        try:
            post_position = float(post_raw)
        except (TypeError, ValueError):
            post_position = None

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
        fee=fee,
        post_position=post_position,
    )


class KalshiBook:
    """Order book for one market, in YES-dollar terms.

    `use_yes_price` mirrors the subscribe flag of the same name: when False
    (Kalshi's current default), no-side updates arrive in NO-leg pricing;
    when True they arrive in YES-leg pricing and are converted back to
    NO-leg internally so the rest of the book math is unchanged."""

    def __init__(self, use_yes_price: bool = False) -> None:
        self.use_yes_price = use_yes_price
        self.yes_bids: dict[float, float] = {}
        self.no_bids: dict[float, float] = {}
        self.last_update: float = 0.0

    # ---- wire application ----

    def apply_snapshot(self, msg: dict, ts: float | None = None) -> None:
        self.yes_bids = self._parse_side(msg, "yes")
        self.no_bids = self._parse_side(msg, "no")
        self.last_update = ts if ts is not None else time.time()

    def _parse_side(self, msg: dict, side: str) -> dict[float, float]:
        arr = msg.get(f"{side}_dollars_fp")
        if arr is None:
            arr = msg.get(side) or []
        out: dict[float, float] = {}
        for price_raw, qty_raw in arr:
            q = norm_qty(qty_raw)
            if q > 0:
                out[self._leg_price(side, norm_price(price_raw))] = q
        return out

    def apply_delta(self, msg: dict, ts: float | None = None) -> None:
        side = msg.get("side")
        price_raw = msg.get("price_dollars", msg.get("price"))
        delta_raw = msg.get("delta_fp", msg.get("delta"))
        if side not in ("yes", "no") or price_raw is None or delta_raw is None:
            return
        price = self._leg_price(side, norm_price(price_raw))
        levels = self.yes_bids if side == "yes" else self.no_bids
        new_qty = levels.get(price, 0.0) + norm_qty(delta_raw)
        if new_qty > 1e-9:
            levels[price] = new_qty
        else:
            levels.pop(price, None)
        self.last_update = ts if ts is not None else time.time()

    def _leg_price(self, side: str, price: float) -> float:
        """Internal storage keeps no-side keys in NO-leg pricing."""
        if side == "no" and self.use_yes_price:
            return round(1.0 - price, 6)
        return price

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
