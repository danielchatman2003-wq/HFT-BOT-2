"""Real-time replica of the CME CF Bitcoin Real-Time Index (BRTI).

Kalshi settles its crypto markets on the mean of the final sixty one-second
BRTI values before close. The licensed BRTI itself is not freely
redistributable in real time, so this module recomputes it from the same
inputs, following CF Benchmarks' published Real Time Index methodology:

1. Consolidate the L2 order books of the constituent exchanges (this bot
   ships adapters for Coinbase, Kraken, Bitstamp and Gemini -- the most
   heavily weighted constituents with free public feeds; LMAX Digital and
   others have no public feed).
2. Build bid/ask price-volume (VWAP-to-depth) curves and their mid curve.
3. "Utilized depth": the largest cumulative volume at which the half-spread
   of the curves stays within 0.5% of the mid price, floored at 1 BTC.
4. Weight the mid curve over [0, utilized depth] by a normalized exponential
   density with decay lambda = 10.3 (heaviest weight near the touch), and
   integrate. Published BRTI is rounded to $0.01.

This is a faithful re-implementation of the public methodology, but it is
an ESTIMATE: our constituent set is a subset, snapshots aren't atomic across
exchanges, and CF applies proprietary data-quality filters. Expect agreement
within a few dollars (usually much less); treat residual disagreement as
model noise -- the vol estimator sees the same series, so pricing stays
internally consistent.

The estimator also keeps a rolling per-second sample history, which powers:
  - the settlement tracker (sum/count of ticks inside a market's final
    60-second window -- the realized part of the settlement average), and
  - the EWMA volatility estimator.
"""
from __future__ import annotations

import bisect
import math
import time
from collections import deque
from dataclasses import dataclass

from src.brti.book import L2Book


@dataclass
class _CumCurve:
    """Cumulative depth curve for one side: VWAP cost to sweep v units."""

    cum_qty: list[float]
    cum_cash: list[float]
    prices: list[float]

    @classmethod
    def from_levels(cls, levels: list[tuple[float, float]]) -> "_CumCurve":
        cq: list[float] = []
        cc: list[float] = []
        ps: list[float] = []
        q_total = 0.0
        cash_total = 0.0
        for price, qty in levels:
            q_total += qty
            cash_total += price * qty
            cq.append(q_total)
            cc.append(cash_total)
            ps.append(price)
        return cls(cq, cc, ps)

    @property
    def total_qty(self) -> float:
        return self.cum_qty[-1] if self.cum_qty else 0.0

    def vwap(self, v: float) -> float:
        """Average price to execute cumulative volume v (clamped to book)."""
        if not self.cum_qty:
            return 0.0
        v = min(max(v, 1e-12), self.total_qty)
        i = bisect.bisect_left(self.cum_qty, v)
        prev_q = self.cum_qty[i - 1] if i > 0 else 0.0
        prev_c = self.cum_cash[i - 1] if i > 0 else 0.0
        cash = prev_c + self.prices[i] * (v - prev_q)
        return cash / v


class BrtiEstimator:
    def __init__(
        self,
        lambda_: float = 10.3,
        deviation_cap: float = 0.005,
        min_depth: float = 1.0,
        feed_stale_ms: float = 5000.0,
        integration_points: int = 101,
        history_seconds: float = 240.0,
    ) -> None:
        self.lambda_ = lambda_
        self.deviation_cap = deviation_cap
        self.min_depth = min_depth
        self.feed_stale_ms = feed_stale_ms
        self.integration_points = integration_points
        self.history_seconds = history_seconds

        self._books: dict[str, L2Book] = {}
        # (ts, value) 1-second samples; values rounded to cents like published BRTI
        self.samples: deque[tuple[float, float]] = deque()
        self.last_value: float | None = None
        self.last_ts: float = 0.0

    # ---- book registration ----

    def register(self, name: str, book: L2Book) -> None:
        self._books[name] = book

    def live_feeds(self, now: float | None = None) -> list[str]:
        now = now if now is not None else time.time()
        return [
            name
            for name, b in self._books.items()
            if b.two_sided and not b.crossed and b.age_ms(now) <= self.feed_stale_ms
        ]

    # ---- index calculation ----

    def compute(self, now: float | None = None) -> float | None:
        now = now if now is not None else time.time()
        live = self.live_feeds(now)
        if not live:
            return None

        bid_levels: list[tuple[float, float]] = []
        ask_levels: list[tuple[float, float]] = []
        for name in live:
            b = self._books[name]
            bid_levels.extend(b.bids.items())
            ask_levels.extend(b.asks.items())
        if not bid_levels or not ask_levels:
            return None

        bid_levels.sort(key=lambda x: -x[0])
        ask_levels.sort(key=lambda x: x[0])
        bid_curve = _CumCurve.from_levels(bid_levels)
        ask_curve = _CumCurve.from_levels(ask_levels)

        best_bid = bid_levels[0][0]
        best_ask = ask_levels[0][0]
        mid0 = 0.5 * (best_bid + best_ask)
        if mid0 <= 0.0:
            return None

        depth_available = min(bid_curve.total_qty, ask_curve.total_qty)
        utilized = self._utilized_depth(bid_curve, ask_curve, mid0, depth_available)

        # Integrate mid-curve against normalized exponential density over
        # x = v / utilized in [0, 1] (midpoint rule; renormalize numerically
        # so discretization error cancels in the weights).
        n = self.integration_points
        lam = self.lambda_
        num = 0.0
        den = 0.0
        for j in range(n):
            x = (j + 0.5) / n
            v = x * utilized
            mid_v = 0.5 * (bid_curve.vwap(v) + ask_curve.vwap(v))
            w = lam * math.exp(-lam * x)
            num += mid_v * w
            den += w
        if den <= 0.0:
            return None
        return num / den

    def _utilized_depth(self, bid_curve: _CumCurve, ask_curve: _CumCurve, mid0: float, depth_available: float) -> float:
        """Largest cumulative volume whose curve half-spread stays within
        deviation_cap of mid, floored at min_depth (per CF methodology),
        capped by what the consolidated book actually holds."""
        if depth_available <= 0.0:
            return self.min_depth
        boundaries = sorted(
            set(
                q
                for q in (bid_curve.cum_qty + ask_curve.cum_qty)
                if q <= depth_available
            )
        )
        if not boundaries or boundaries[-1] < depth_available:
            boundaries.append(depth_available)
        utilized = 0.0
        for v in boundaries:
            half_spread = 0.5 * (ask_curve.vwap(v) - bid_curve.vwap(v))
            if half_spread / mid0 <= self.deviation_cap:
                utilized = v
            else:
                break
        utilized = max(utilized, self.min_depth)
        return min(utilized, depth_available) if depth_available > 0 else utilized

    # ---- 1 Hz sampling / settlement window ----

    def sample(self, now: float | None = None) -> float | None:
        """Compute, round to cents (published BRTI precision), and record."""
        now = now if now is not None else time.time()
        value = self.compute(now)
        if value is None:
            return None
        value = round(value, 2)
        self.samples.append((now, value))
        cutoff = now - self.history_seconds
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        self.last_value = value
        self.last_ts = now
        return value

    def age_ms(self, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        if self.last_ts == 0.0:
            return float("inf")
        return (now - self.last_ts) * 1000.0

    def window_stats(self, close_ts: float, window_s: float = 60.0, now: float | None = None) -> tuple[float, int]:
        """(sum, count) of samples inside a market's settlement window, up to
        now. Kalshi's official window semantics (per the AsyncAPI spec) are
        `(close - 60s, close]`: the start-boundary tick is excluded and the
        close tick is included."""
        now = now if now is not None else time.time()
        start = close_ts - window_s
        total = 0.0
        count = 0
        for ts, v in self.samples:
            if start < ts <= close_ts and ts <= now:
                total += v
                count += 1
        return total, count
