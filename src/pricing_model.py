"""Fair-value model for Kalshi's BTC 15-minute "above strike" binary contracts.

A Kalshi YES contract on "BTC price above $K at time T" pays $1 if true, $0
otherwise. Modeling BTC's short-horizon path as geometric Brownian motion
with zero drift (a standard, defensible assumption over a 15-minute window --
you are not trying to predict direction, only to price the probability
consistently with the market), the fair probability of finishing above K is:

    d2 = (ln(S / K) - 0.5 * sigma^2 * T) / (sigma * sqrt(T))
    P(S_T > K) = N(d2)

where S is spot, sigma is annualized volatility, T is time to expiry in years.

This is the SAME probability that should equal the fair YES price (in
dollars). The trading edge, if any, comes from comparing this model price to
the market's current bid/ask -- not from predicting where BTC is headed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.stats import norm


@dataclass(frozen=True)
class FairValue:
    prob_yes: float  # model probability that YES resolves true, in [0, 1]

    @property
    def prob_no(self) -> float:
        return 1.0 - self.prob_yes

    @property
    def yes_price_cents(self) -> float:
        return self.prob_yes * 100

    @property
    def no_price_cents(self) -> float:
        return self.prob_no * 100


def fair_value_above_strike(
    spot: float,
    strike: float,
    seconds_to_expiry: float,
    annualized_vol: float,
) -> FairValue:
    """Probability BTC is above `strike` at expiry."""
    if seconds_to_expiry <= 0:
        return FairValue(prob_yes=1.0 if spot > strike else 0.0)
    if annualized_vol <= 0:
        return FairValue(prob_yes=1.0 if spot > strike else 0.0)

    t_years = seconds_to_expiry / (365 * 24 * 3600)
    sigma_sqrt_t = annualized_vol * math.sqrt(t_years)
    if sigma_sqrt_t <= 0:
        return FairValue(prob_yes=1.0 if spot > strike else 0.0)

    d2 = (math.log(spot / strike) - 0.5 * annualized_vol**2 * t_years) / sigma_sqrt_t
    prob_yes = float(norm.cdf(d2))
    return FairValue(prob_yes=prob_yes)


def fair_value_between(
    spot: float,
    lower: float,
    upper: float,
    seconds_to_expiry: float,
    annualized_vol: float,
) -> FairValue:
    """Probability BTC finishes strictly between `lower` and `upper` at expiry
    (used for Kalshi's range/bracket markets)."""
    above_lower = fair_value_above_strike(spot, lower, seconds_to_expiry, annualized_vol)
    above_upper = fair_value_above_strike(spot, upper, seconds_to_expiry, annualized_vol)
    prob_yes = max(0.0, above_lower.prob_yes - above_upper.prob_yes)
    return FairValue(prob_yes=prob_yes)
