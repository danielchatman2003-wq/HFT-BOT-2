"""Order execution and position accounting, in live and paper flavors.

Both flavors expose the same interface to the strategy:
    place(...) -> OpenOrder | None
    cancel(order), cancel_all(ticker=None)
    on_fill(FillEvent)          (live: from the ws fill channel)
    settle(ticker, result_yes)  (realizes settlement PnL)
plus position/collateral views used by risk checks.

Positions use signed-YES average-cost accounting: pos > 0 is long YES at
avg_price; pos < 0 is short YES (equivalently long NO) where avg_price is
the average YES-price received. Settlement pays pos * (result - avg_price).

Paper mode simulates against LIVE Kalshi data with no orders ever sent:
  - aggressive orders fill by walking the real displayed book;
  - resting quotes fill when a real trade prints through the quote price or
    the opposite side of the book crosses it. This ignores queue priority
    (you are assumed to be at the front), so paper maker fills are
    OPTIMISTIC -- treat paper PnL as an upper bound, not an expectation.
"""
from __future__ import annotations

import csv
import itertools
import logging
import os
import time
import uuid
from dataclasses import dataclass, field

from src.config import Config
from src.fees import order_fee
from src.kalshi.orderbook import FillEvent, KalshiBook
from src.kalshi.rest import KalshiAPIError, KalshiRest
from src.risk import RiskManager

logger = logging.getLogger("exec")


@dataclass
class OpenOrder:
    ticker: str
    intent: str                # "buy_yes" | "sell_yes"
    price: float               # YES-dollars
    count: int
    purpose: str               # "bid" | "ask" | "take"
    post_only: bool = False
    order_id: str = ""
    client_order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    filled: int = 0
    created_ts: float = field(default_factory=time.time)

    @property
    def remaining(self) -> int:
        return max(0, self.count - self.filled)

    @property
    def direction(self) -> int:
        return 1 if self.intent == "buy_yes" else -1


@dataclass
class Position:
    pos: int = 0               # signed YES contracts
    avg_price: float = 0.0     # YES-dollars
    realized: float = 0.0      # realized PnL (excl. fees)
    fees: float = 0.0
    settled: bool = False

    def apply_fill(self, signed_count: int, price: float) -> float:
        """Average-cost accounting; returns realized PnL delta (excl. fees)."""
        realized_delta = 0.0
        remaining = signed_count
        if self.pos != 0 and (self.pos > 0) != (remaining > 0):
            n_close = min(abs(remaining), abs(self.pos))
            side = 1 if self.pos > 0 else -1
            realized_delta = side * (price - self.avg_price) * n_close
            self.pos -= side * n_close
            remaining += side * n_close
            if self.pos == 0:
                self.avg_price = 0.0
        if remaining != 0:
            new_abs = abs(self.pos) + abs(remaining)
            self.avg_price = (abs(self.pos) * self.avg_price + abs(remaining) * price) / new_abs
            self.pos += remaining
        self.realized += realized_delta
        return realized_delta

    def settle(self, result_yes: bool) -> float:
        payout_price = 1.0 if result_yes else 0.0
        realized_delta = self.pos * (payout_price - self.avg_price)
        self.realized += realized_delta
        self.pos = 0
        self.avg_price = 0.0
        self.settled = True
        return realized_delta

    @property
    def collateral(self) -> float:
        if self.pos > 0:
            return self.pos * self.avg_price
        return -self.pos * (1.0 - self.avg_price)


class TradeLog:
    """Append-only CSV of every fill and settlement -- the raw material for
    validating edge offline. Disabled when path is empty."""

    FIELDS = ["ts", "kind", "ticker", "signed_count", "price", "fee", "pos_after", "realized_delta"]

    def __init__(self, path: str) -> None:
        self.path = path
        self._writer: csv.DictWriter | None = None

    def log(self, kind: str, ticker: str, signed_count: int, price: float,
            fee: float, pos_after: int, realized_delta: float) -> None:
        if not self.path:
            return
        try:
            if self._writer is None:
                new_file = not os.path.exists(self.path)
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                f = open(self.path, "a", newline="")
                self._writer = csv.DictWriter(f, fieldnames=self.FIELDS)
                if new_file:
                    self._writer.writeheader()
                self._file = f
            self._writer.writerow({
                "ts": f"{time.time():.3f}", "kind": kind, "ticker": ticker,
                "signed_count": signed_count, "price": f"{price:.4f}",
                "fee": f"{fee:.2f}", "pos_after": pos_after,
                "realized_delta": f"{realized_delta:.2f}",
            })
            self._file.flush()
        except OSError as exc:
            logger.warning("trade log write failed (%s); disabling", exc)
            self.path = ""


class ExecutionBase:
    def __init__(self, cfg: Config, risk: RiskManager) -> None:
        self.cfg = cfg
        self.risk = risk
        self.orders: dict[str, OpenOrder] = {}       # client_order_id -> order
        self.positions: dict[str, Position] = {}
        self.fills_log: list[FillEvent] = []
        self.trade_log = TradeLog(cfg.trade_log_path)

    # ---- views ----

    def position(self, ticker: str) -> Position:
        return self.positions.setdefault(ticker, Position())

    def collateral_in_use(self) -> float:
        return sum(p.collateral for p in self.positions.values())

    def open_orders(self, ticker: str | None = None, purpose: str | None = None) -> list[OpenOrder]:
        out = []
        for o in self.orders.values():
            if ticker is not None and o.ticker != ticker:
                continue
            if purpose is not None and o.purpose != purpose:
                continue
            out.append(o)
        return out

    def total_realized(self) -> float:
        return sum(p.realized - p.fees for p in self.positions.values())

    # ---- fills / settlement (shared) ----

    def on_fill(self, fill: FillEvent, fee_rate: float | None = None) -> None:
        order = self._match_order(fill)
        if order is not None:
            order.filled += abs(fill.signed_count)
            if order.remaining <= 0:
                self.orders.pop(order.client_order_id, None)
        if fee_rate is None:
            fee_rate = self.cfg.taker_fee_rate if fill.is_taker else self.cfg.maker_fee_rate
        pos = self.position(fill.ticker)
        realized_delta = pos.apply_fill(fill.signed_count, fill.price)
        fee = order_fee(fill.price, abs(fill.signed_count), fee_rate)
        pos.fees += fee
        if realized_delta or fee:
            self.risk.on_realized_pnl(realized_delta - fee)
        self.fills_log.append(fill)
        self.trade_log.log("fill", fill.ticker, fill.signed_count, fill.price, fee, pos.pos, realized_delta)
        logger.info(
            "FILL %s %+d @ %.2fc (%s, fee $%.2f) pos=%+d",
            fill.ticker, fill.signed_count, fill.price * 100,
            "taker" if fill.is_taker else "maker", fee, pos.pos,
        )

    def _match_order(self, fill: FillEvent) -> OpenOrder | None:
        if fill.client_order_id and fill.client_order_id in self.orders:
            return self.orders[fill.client_order_id]
        if fill.order_id:
            for o in self.orders.values():
                if o.order_id == fill.order_id:
                    return o
        return None

    def settle(self, ticker: str, result_yes: bool) -> float:
        pos = self.position(ticker)
        had = pos.pos
        realized_delta = pos.settle(result_yes)
        if had != 0:
            self.risk.on_realized_pnl(realized_delta)
            self.trade_log.log("settle", ticker, -had, 1.0 if result_yes else 0.0, 0.0, 0, realized_delta)
            logger.info(
                "SETTLED %s -> %s: %+d contracts, pnl $%.2f (fees $%.2f, market total $%.2f)",
                ticker, "YES" if result_yes else "NO", had, realized_delta, pos.fees, pos.realized - pos.fees,
            )
        return realized_delta

    # ---- to implement ----

    async def place(self, ticker: str, intent: str, price: float, count: int,
                    tif: str = "gtc", post_only: bool = False, purpose: str = "take") -> OpenOrder | None:
        raise NotImplementedError

    async def cancel(self, order: OpenOrder) -> None:
        raise NotImplementedError

    async def cancel_all(self, ticker: str | None = None) -> None:
        for o in list(self.open_orders(ticker)):
            await self.cancel(o)


class LiveExecution(ExecutionBase):
    def __init__(self, cfg: Config, risk: RiskManager, rest: KalshiRest) -> None:
        super().__init__(cfg, risk)
        self.rest = rest

    async def place(self, ticker: str, intent: str, price: float, count: int,
                    tif: str = "gtc", post_only: bool = False, purpose: str = "take") -> OpenOrder | None:
        order = OpenOrder(ticker=ticker, intent=intent, price=price, count=count,
                          purpose=purpose, post_only=post_only)
        pos = self.position(ticker).pos
        closing = (intent == "sell_yes" and pos > 0) or (intent == "buy_yes" and pos < 0)
        try:
            resp = await self.rest.create_order(
                ticker=ticker,
                side_intent=intent,
                price=price,
                count=count,
                tif=tif,
                post_only=post_only,
                expire_in_s=self.cfg.quote_ttl_s if purpose in ("bid", "ask") else None,
                client_order_id=order.client_order_id,
                closing=closing,
            )
        except KalshiAPIError as exc:
            # Post-only orders that would cross are rejected by design; not an error.
            if post_only and exc.status in (400, 409):
                logger.debug("post-only rejected (would cross): %s", exc)
                return None
            self.risk.record_order_error()
            logger.error("order rejected %s %s %d@%.2f: %s", ticker, intent, count, price, exc)
            return None
        self.risk.record_order_ok()
        order.order_id = str(resp.get("order_id") or resp.get("id") or "")
        status = str(resp.get("status", ""))
        # IOC orders never rest; whatever filled arrives on the fill channel.
        if tif != "ioc" and status not in ("canceled", "executed"):
            self.orders[order.client_order_id] = order
        return order

    async def cancel(self, order: OpenOrder) -> None:
        self.orders.pop(order.client_order_id, None)
        if not order.order_id:
            return
        try:
            await self.rest.cancel_order(order.order_id)
        except KalshiAPIError as exc:
            if exc.status != 404:  # 404 = already gone (filled/expired)
                logger.warning("cancel %s failed: %s", order.order_id, exc)

    async def cancel_all(self, ticker: str | None = None) -> None:
        targets = list(self.open_orders(ticker))
        for o in targets:
            self.orders.pop(o.client_order_id, None)
        ids = [o.order_id for o in targets if o.order_id]
        if ids:
            await self.rest.batch_cancel(ids)


class PaperExecution(ExecutionBase):
    """Simulated fills against live Kalshi market data. Nothing is ever sent."""

    def __init__(self, cfg: Config, risk: RiskManager, books: dict[str, KalshiBook]) -> None:
        super().__init__(cfg, risk)
        self.books = books
        self._id_seq = itertools.count(1)

    async def place(self, ticker: str, intent: str, price: float, count: int,
                    tif: str = "gtc", post_only: bool = False, purpose: str = "take") -> OpenOrder | None:
        order = OpenOrder(ticker=ticker, intent=intent, price=price, count=count,
                          purpose=purpose, post_only=post_only,
                          order_id=f"paper-{next(self._id_seq)}")
        book = self.books.get(ticker)
        crossing = self._crossable(order, book)
        if post_only and crossing:
            return None  # Kalshi rejects post-only orders that would cross
        if crossing and book is not None:
            walk_side = "buy" if intent == "buy_yes" else "sell"
            avg, filled = book.walk(walk_side, count, limit_price=price)
            if filled > 0:
                self._sim_fill(order, avg, filled, is_taker=True)
        if tif == "ioc":
            # _sim_fill registers the order for fill accounting; an IOC
            # remainder must not rest, so drop it again.
            self.orders.pop(order.client_order_id, None)
            return order
        if order.remaining > 0:
            self.orders[order.client_order_id] = order
        return order

    async def cancel(self, order: OpenOrder) -> None:
        self.orders.pop(order.client_order_id, None)

    # ---- market-data hooks (called by the bot loop) ----

    def on_market_trade(self, trade: dict) -> None:
        """A real trade printed: fill any resting sim quote it crosses."""
        ticker, price, size = trade["ticker"], trade["yes_price"], trade["count"]
        for order in list(self.open_orders(ticker)):
            if size <= 0:
                break
            if order.intent == "buy_yes" and price <= order.price + 1e-9:
                n = min(order.remaining, size)
                self._sim_fill(order, order.price, n, is_taker=False)
                size -= n
            elif order.intent == "sell_yes" and price >= order.price - 1e-9:
                n = min(order.remaining, size)
                self._sim_fill(order, order.price, n, is_taker=False)
                size -= n

    def on_book_update(self, ticker: str) -> None:
        """If the opposite side of the real book crosses a resting quote,
        someone would have traded with us; fill it."""
        book = self.books.get(ticker)
        if book is None:
            return
        for order in list(self.open_orders(ticker)):
            if order.intent == "buy_yes":
                ask = book.best_ask()
                if ask is not None and ask <= order.price + 1e-9:
                    self._sim_fill(order, order.price, order.remaining, is_taker=False)
            else:
                bid = book.best_bid()
                if bid is not None and bid >= order.price - 1e-9:
                    self._sim_fill(order, order.price, order.remaining, is_taker=False)

    def expire_stale_quotes(self, now: float | None = None) -> None:
        """Mirror the server-side TTL that live quotes carry."""
        now = now if now is not None else time.time()
        for order in list(self.orders.values()):
            if order.purpose in ("bid", "ask") and now - order.created_ts > self.cfg.quote_ttl_s:
                self.orders.pop(order.client_order_id, None)

    # ---- internals ----

    @staticmethod
    def _crossable(order: OpenOrder, book: KalshiBook | None) -> bool:
        if book is None:
            return False
        if order.intent == "buy_yes":
            ask = book.best_ask()
            return ask is not None and ask <= order.price + 1e-9
        bid = book.best_bid()
        return bid is not None and bid >= order.price - 1e-9

    def _sim_fill(self, order: OpenOrder, price: float, count: int, is_taker: bool) -> None:
        if count <= 0:
            return
        fill = FillEvent(
            ticker=order.ticker,
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            signed_count=order.direction * count,
            price=price,
            is_taker=is_taker,
            ts=time.time(),
        )
        # Register the order so on_fill can decrement it, then route through
        # the exact same accounting path live fills take.
        self.orders.setdefault(order.client_order_id, order)
        tag = "[PAPER-TAKE]" if is_taker else "[PAPER-MAKE]"
        logger.info("%s %s %s %d @ %.2fc", tag, order.ticker, order.intent, count, price * 100)
        self.on_fill(fill)
