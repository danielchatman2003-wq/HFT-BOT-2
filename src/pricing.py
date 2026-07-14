"""Fair-value model for Kalshi's 15-minute crypto Up/Down contracts.

Contract: YES ("Up") pays $1 if the settlement value exceeds the strike
("price to beat", the market's `floor_strike`). Kalshi settles crypto
markets on the MEAN OF THE FINAL 60 ONE-SECOND CF BRTI prints before close,
not the instantaneous close -- and that averaging changes the math in two
ways this model accounts for and most naive bots miss:

1. Before the averaging window: the variance of a time-average of Brownian
   motion over a window of width w starting a seconds from now is
   sigma^2 * (a + w/3), not sigma^2 * (a + w). The settlement value is
   meaningfully less volatile than the spot close.

2. Inside the final minute: part of the average is already REALIZED. With m
   of the 60 ticks observed summing to R, YES wins iff the mean of the
   remaining n ticks exceeds K_adj = (60*K - R) / n. As ticks lock in,
   uncertainty collapses deterministically -- the market often keeps
   pricing residual uncertainty that no longer exists.

Model: zero-drift geometric Brownian motion for the index at second-scale
horizons; the arithmetic average is approximated as lognormal (exact to
O(sigma^2 * tau), which at 15-minute crypto vols is < 1e-5 -- far below
exchange-composition noise in the BRTI estimate itself). Volatility enters
per-sqrt-second; nothing here is annualized internally.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0
_SQRT_SPY = math.sqrt(SECONDS_PER_YEAR)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def annual_to_per_sqrt_sec(sigma_annual: float) -> float:
    return sigma_annual / _SQRT_SPY


def per_sqrt_sec_to_annual(sigma_s: float) -> float:
    return sigma_s * _SQRT_SPY


def prob_settle_above(
    spot: float,
    strike: float,
    seconds_to_close: float,
    sigma_s: float,
    avg_window_s: float = 60.0,
    realized_sum: float | None = None,
    realized_count: int | None = None,
    total_ticks: int = 60,
) -> float:
    """P(settlement average > strike) for an Up/Down contract.

    spot          -- current index estimate (BRTI replica)
    strike        -- price to beat (market's floor_strike)
    seconds_to_close -- time until market close
    sigma_s       -- volatility per sqrt-second (NOT annualized)
    realized_sum/realized_count -- sum and count of settlement-window ticks
        already observed (only meaningful once inside the final minute).
    """
    if spot <= 0.0 or strike <= 0.0:
        raise ValueError("spot and strike must be positive")

    tau = seconds_to_close
    w = avg_window_s

    # Window over: outcome is whatever the average already is.
    if tau <= 0.0:
        if realized_count:
            return 1.0 if (realized_sum / realized_count) > strike else 0.0
        return 1.0 if spot > strike else 0.0

    if sigma_s <= 0.0:
        return 1.0 if spot > strike else 0.0

    if tau > w:
        # Entire averaging window is in the future.
        a = tau - w
        var = sigma_s * sigma_s * (a + w / 3.0)
        mu = math.log(spot / strike) - 0.5 * sigma_s * sigma_s * (a + w / 2.0)
        if var <= 1e-18:
            return 1.0 if mu > 0 else 0.0
        return _clamp01(norm_cdf(mu / math.sqrt(var)))

    # Inside the averaging window.
    if realized_count is not None and realized_sum is not None and realized_count > 0:
        m = min(realized_count, total_ticks)
        n_rem = total_ticks - m
        if n_rem <= 0:
            return 1.0 if (realized_sum / m) > strike else 0.0
        k_adj = (total_ticks * strike - realized_sum) / n_rem
        if k_adj <= 0.0:
            return 1.0  # already locked in: even a zero price would settle Up
        r = tau
        var = sigma_s * sigma_s * (r / 3.0)
        mu = math.log(spot / k_adj) - 0.25 * sigma_s * sigma_s * r
        if var <= 1e-18:
            return 1.0 if mu > 0 else 0.0
        return _clamp01(norm_cdf(mu / math.sqrt(var)))

    # Inside the window but no realized ticks supplied (estimator gap):
    # price the remaining-average against the full strike. Biased, but the
    # staleness guards should be halting trading in this state anyway.
    r = tau
    var = sigma_s * sigma_s * (r / 3.0)
    mu = math.log(spot / strike) - 0.25 * sigma_s * sigma_s * r
    if var <= 1e-18:
        return 1.0 if mu > 0 else 0.0
    return _clamp01(norm_cdf(mu / math.sqrt(var)))


def prob_sensitivity_per_log_spot(
    spot: float,
    strike: float,
    seconds_to_close: float,
    sigma_s: float,
    avg_window_s: float = 60.0,
) -> float:
    """d(prob)/d(ln spot) -- used to convert index vol into probability-space
    vol when sizing maker quote widths. Uses the pre-window variance shape
    (good enough for quoting; quoting stops before the final minute)."""
    tau = max(seconds_to_close, 1e-6)
    w = min(avg_window_s, tau)
    a = max(tau - avg_window_s, 0.0)
    var = sigma_s * sigma_s * (a + w / 3.0) if tau > avg_window_s else sigma_s * sigma_s * (tau / 3.0)
    sd = math.sqrt(max(var, 1e-18))
    z = math.log(spot / strike) / sd
    pdf = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
    return pdf / sd


def _clamp01(x: float) -> float:
    return min(1.0, max(0.0, x))


@dataclass
class EwmaVol:
    """EWMA volatility of 1-second log returns, kept per-sqrt-second.

    Returns are normalized by sqrt(dt) so irregular sampling doesn't bias the
    estimate; single prints beyond winsor_k sigmas are clipped so one glitchy
    feed tick can't blow up quote widths (real regime shifts arrive as many
    ticks, which pass through)."""

    half_life_s: float = 300.0
    min_samples: int = 45
    floor_annual: float = 0.15
    cap_annual: float = 4.0
    winsor_k: float = 8.0

    _last_ts: float | None = None
    _last_price: float | None = None
    _var: float = 0.0  # per-second variance of log returns
    _n: int = 0

    def update(self, ts: float, price: float) -> None:
        if price <= 0.0:
            return
        if self._last_ts is None or self._last_price is None:
            self._last_ts, self._last_price = ts, price
            return
        dt = ts - self._last_ts
        if dt <= 0.0 or dt > 30.0:
            # Clock jump or long gap: restart the pairing, keep the variance.
            self._last_ts, self._last_price = ts, price
            return
        r = math.log(price / self._last_price) / math.sqrt(dt)
        if self._n >= self.min_samples and self._var > 0.0:
            cap = self.winsor_k * math.sqrt(self._var)
            r = max(-cap, min(cap, r))
        alpha = 1.0 - 0.5 ** (dt / self.half_life_s)
        self._var = (1.0 - alpha) * self._var + alpha * r * r
        self._n += 1
        self._last_ts, self._last_price = ts, price

    @property
    def ready(self) -> bool:
        return self._n >= self.min_samples and self._var > 0.0

    @property
    def sigma_s(self) -> float:
        """Volatility per sqrt-second, clamped to [floor, cap] (annualized bounds)."""
        raw = math.sqrt(self._var) if self._var > 0.0 else 0.0
        lo = annual_to_per_sqrt_sec(self.floor_annual)
        hi = annual_to_per_sqrt_sec(self.cap_annual)
        return min(hi, max(lo, raw))

    @property
    def sigma_annual(self) -> float:
        return per_sqrt_sec_to_annual(self.sigma_s)
