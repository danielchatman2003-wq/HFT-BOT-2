"""Event-driven maker/taker strategy for Kalshi 15-minute Up/Down markets.

On every BRTI tick, Kalshi book change, fill, or timer tick, each active
market is re-evaluated:

TAKER  -- if the model's fair probability disagrees with the displayed bid
or ask by more than the taker fee plus MIN_TAKER_EDGE, sweep the displayed
liquidity with an IOC order (sized by risk-capped Kelly). This is the
"someone left a stale quote" trade, and it is the main way the settlement-
averaging math (especially inside the final minute) gets monetized.

MAKER  -- otherwise, rest post-only quotes around fair value. The half
spread adapts to volatility (how far fair value can plausibly move before
we can reprice) and never quotes tighter than maker fee + MIN_MAKER_EDGE.
Inventory skews both quotes toward flattening. Quotes carry a short
server-side TTL so a crashed bot bleeds out of the book by itself.

Near the close, quoting stops first (MAKER_STOP_BEFORE_CLOSE_S), taking
stops last (TAKER_STOP_BEFORE_CLOSE_S), and positions ride to settlement --
these contracts cash-settle minutes after close, so inventory is time-boxed
by construction.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from src.brti.index import BrtiEstimator
from src.config import Config
from src.execution import ExecutionBase
from src.fees import fee_per_contract
from src.kalshi.orderbook import KalshiBook
from src.pricing import EwmaVol, prob_sensitivity_per_log_spot, prob_settle_above
from src.risk import RiskManager

logger = logging.getLogger("strategy")


@dataclass
class MarketState:
    ticker: str
    close_ts: float
    strike: float | None = None
    settled: bool = False
    last_take_ts: dict[int, float] = field(default_factory=dict)      # direction -> ts
    last_replace_ts: dict[str, float] = field(default_factory=dict)   # "bid"/"ask" -> ts
    takes: int = 0
    last_fair: float | None = None

    def seconds_to_close(self, now: float) -> float:
        return self.close_ts - now


class UpDownStrategy:
    def __init__(
        self,
        cfg: Config,
        estimator: BrtiEstimator,
        vol: EwmaVol,
        books: dict[str, KalshiBook],
        execution: ExecutionBase,
        risk: RiskManager,
    ) -> None:
        self.cfg = cfg
        self.estimator = estimator
        self.vol = vol
        self.books = books
        self.execution = execution
        self.risk = risk
        self.markets: dict[str, MarketState] = {}

    # ---- market lifecycle (driven by the bot's discovery loop) ----

    def upsert_market(self, ticker: str, close_ts: float, strike: float | None) -> None:
        m = self.markets.get(ticker)
        if m is None:
            m = MarketState(ticker=ticker, close_ts=close_ts, strike=strike)
            self.markets[ticker] = m
            logger.info(
                "tracking %s: closes in %.0fs, strike=%s",
                ticker, close_ts - time.time(), f"{strike:,.2f}" if strike else "pending",
            )
        else:
            m.close_ts = close_ts
            if strike is not None and m.strike != strike:
                m.strike = strike
                logger.info("%s strike (price to beat): %s", ticker, f"{strike:,.2f}")

    def drop_market(self, ticker: str) -> None:
        self.markets.pop(ticker, None)

    async def settle_market(self, ticker: str, result_yes: bool) -> None:
        m = self.markets.get(ticker)
        if m is None or m.settled:
            return
        m.settled = True
        await self.execution.cancel_all(ticker)
        self.execution.settle(ticker, result_yes)

    # ---- event handling ----

    async def on_event(self, kind: str, payload) -> None:
        now = time.time()
        if kind in ("brti_tick", "timer"):
            for m in list(self.markets.values()):
                await self.evaluate(m, now)
        elif kind == "kalshi_book":
            m = self.markets.get(payload)
            if m:
                await self.evaluate(m, now)
        elif kind == "fill":
            m = self.markets.get(payload.ticker)
            if m:
                await self.evaluate(m, now)
        elif kind == "kalshi_status" and payload == "disconnected":
            # Books are about to be stale; pull local quote state rather than
            # trust it (live server-side TTLs bleed the real orders off).
            await self._cancel_quotes_everywhere()

    async def evaluate(self, m: MarketState, now: float) -> None:
        if m.settled:
            return
        tau = m.seconds_to_close(now)
        if tau <= 0:
            await self._cancel_quotes(m)
            return

        book = self.books.get(m.ticker)
        if self.risk.halted or book is None or not self.risk.can_trade(
            self.estimator.age_ms(now), book.age_ms(now), self.vol.ready
        ):
            await self._cancel_quotes(m)
            return
        if m.strike is None or self.estimator.last_value is None:
            return

        spot = self.estimator.last_value
        realized_sum: float | None = None
        realized_count: int | None = None
        if tau <= self.cfg.settlement_window_s + 5.0:
            realized_sum, realized_count = self.estimator.window_stats(
                m.close_ts, self.cfg.settlement_window_s, now
            )
            if realized_count == 0:
                realized_sum, realized_count = None, None

        fair = prob_settle_above(
            spot=spot,
            strike=m.strike,
            seconds_to_close=tau,
            sigma_s=self.vol.sigma_s,
            avg_window_s=self.cfg.settlement_window_s,
            realized_sum=realized_sum,
            realized_count=realized_count,
            total_ticks=self.cfg.settlement_ticks,
        )
        m.last_fair = fair

        if self.cfg.enable_taker and tau > self.cfg.taker_stop_before_close_s:
            await self._maybe_take(m, book, fair, now)
        if self.cfg.enable_maker:
            if tau > self.cfg.maker_stop_before_close_s and self._quotable(fair):
                await self._sync_quotes(m, book, fair, tau, now)
            else:
                await self._cancel_quotes(m)

    # ---- taker ----

    async def _maybe_take(self, m: MarketState, book: KalshiBook, fair: float, now: float) -> None:
        bid, ask = book.bbo()
        pos = self.execution.position(m.ticker).pos
        collateral = self.execution.collateral_in_use()

        # Buy YES when fair exceeds the ask by fee + edge.
        if ask is not None and 0.0 < ask < 1.0 and now - m.last_take_ts.get(1, 0.0) >= self.cfg.taker_cooldown_s:
            edge = fair - ask - fee_per_contract(ask, self.cfg.taker_fee_rate)
            if edge >= self.cfg.min_taker_edge:
                size = self.risk.taker_size(fair, ask, +1, pos, collateral)
                size = min(size, book.walk("buy", size, limit_price=ask)[1]) if size > 0 else 0
                if size > 0:
                    m.last_take_ts[1] = now
                    m.takes += 1
                    logger.info(
                        "TAKE BUY %s %d @ %.2fc (fair=%.3f edge=%.2fc)",
                        m.ticker, size, ask * 100, fair, edge * 100,
                    )
                    await self.execution.place(m.ticker, "buy_yes", ask, size, tif="ioc", purpose="take")

        # Sell YES (unload longs or open NO exposure) when the bid exceeds fair.
        if bid is not None and 0.0 < bid < 1.0 and now - m.last_take_ts.get(-1, 0.0) >= self.cfg.taker_cooldown_s:
            edge = bid - fair - fee_per_contract(bid, self.cfg.taker_fee_rate)
            if edge >= self.cfg.min_taker_edge:
                size = self.risk.taker_size(fair, bid, -1, pos, collateral)
                size = min(size, book.walk("sell", size, limit_price=bid)[1]) if size > 0 else 0
                if size > 0:
                    m.last_take_ts[-1] = now
                    m.takes += 1
                    logger.info(
                        "TAKE SELL %s %d @ %.2fc (fair=%.3f edge=%.2fc)",
                        m.ticker, size, bid * 100, fair, edge * 100,
                    )
                    await self.execution.place(m.ticker, "sell_yes", bid, size, tif="ioc", purpose="take")

    # ---- maker ----

    def _quotable(self, fair: float) -> bool:
        hi = self.cfg.extreme_prob_no_quote
        return (1.0 - hi) <= fair <= hi

    async def _sync_quotes(self, m: MarketState, book: KalshiBook, fair: float, tau: float, now: float) -> None:
        tick = self.cfg.price_tick
        pos = self.execution.position(m.ticker).pos
        collateral = self.execution.collateral_in_use()

        # How far can fair value plausibly move before we can reprice?
        sigma_move = (
            prob_sensitivity_per_log_spot(
                self.estimator.last_value, m.strike, tau, self.vol.sigma_s, self.cfg.settlement_window_s
            )
            * self.vol.sigma_s
            * (self.cfg.maker_horizon_s ** 0.5)
        )
        half_spread = max(
            self.cfg.min_maker_edge + fee_per_contract(fair, self.cfg.maker_fee_rate, self.cfg.quote_size),
            self.cfg.maker_vol_mult * sigma_move,
        )
        skew = half_spread * (pos / self.cfg.max_pos_per_market) if self.cfg.max_pos_per_market else 0.0

        bid_target = self._floor_tick(fair - half_spread - skew)
        ask_target = self._ceil_tick(fair + half_spread - skew)
        mkt_bid, mkt_ask = book.bbo()
        if mkt_ask is not None:
            bid_target = min(bid_target, self._floor_tick(mkt_ask - tick))  # never cross
        if mkt_bid is not None:
            ask_target = max(ask_target, self._ceil_tick(mkt_bid + tick))
        bid_target = min(max(bid_target, tick), 1.0 - 2 * tick)
        ask_target = min(max(ask_target, bid_target + tick), 1.0 - tick)

        bid_size = self.risk.quote_size(+1, pos, collateral)
        ask_size = self.risk.quote_size(-1, pos, collateral)

        await self._sync_side(m, "bid", "buy_yes", bid_target, bid_size, now)
        await self._sync_side(m, "ask", "sell_yes", ask_target, ask_size, now)

    async def _sync_side(self, m: MarketState, side: str, intent: str,
                         target_price: float, target_size: int, now: float) -> None:
        existing = self.execution.open_orders(m.ticker, purpose=side)
        current = existing[0] if existing else None

        if target_size <= 0:
            if current is not None:
                await self.execution.cancel(current)
            return

        needs_replace = current is None
        if current is not None:
            drifted = abs(current.price - target_price) >= self.cfg.reprice_threshold
            depleted = current.remaining < max(1, target_size // 2)
            expiring = now - current.created_ts > max(self.cfg.quote_ttl_s - 2.0, 1.0)
            needs_replace = drifted or depleted or expiring
        if not needs_replace:
            return
        if now - m.last_replace_ts.get(side, 0.0) < self.cfg.min_replace_interval_ms / 1000.0:
            return

        m.last_replace_ts[side] = now
        for o in existing:
            await self.execution.cancel(o)
        await self.execution.place(
            m.ticker, intent, target_price, target_size,
            tif="gtc", post_only=True, purpose=side,
        )

    async def _cancel_quotes(self, m: MarketState) -> None:
        for side in ("bid", "ask"):
            for o in self.execution.open_orders(m.ticker, purpose=side):
                await self.execution.cancel(o)

    async def _cancel_quotes_everywhere(self) -> None:
        for m in self.markets.values():
            await self._cancel_quotes(m)

    def _floor_tick(self, p: float) -> float:
        t = self.cfg.price_tick
        return round((p // t) * t, 4) if t > 0 else p

    def _ceil_tick(self, p: float) -> float:
        t = self.cfg.price_tick
        if t <= 0:
            return p
        n = p / t
        return round((int(n) if abs(n - int(n)) < 1e-9 else int(n) + 1) * t, 4)
