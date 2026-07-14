"""Tests for the Up/Down settlement-average pricing model."""
import math

import pytest

from src.pricing import (
    EwmaVol,
    annual_to_per_sqrt_sec,
    norm_cdf,
    prob_settle_above,
)

SIGMA = annual_to_per_sqrt_sec(0.60)  # 60% annualized, realistic for BTC


def test_atm_is_near_half():
    p = prob_settle_above(100_000.0, 100_000.0, 900.0, SIGMA)
    assert 0.49 < p < 0.5001  # tiny drift correction pulls it just under 0.5


def test_monotonic_in_spot():
    ps = [prob_settle_above(s, 100_000.0, 300.0, SIGMA) for s in (99_800, 99_950, 100_000, 100_050, 100_200)]
    assert ps == sorted(ps)
    assert ps[0] < 0.5 < ps[-1]


def test_zero_vol_is_step():
    assert prob_settle_above(100_100.0, 100_000.0, 300.0, 0.0) == 1.0
    assert prob_settle_above(99_900.0, 100_000.0, 300.0, 0.0) == 0.0


def test_expired_uses_realized_average():
    assert prob_settle_above(99_000.0, 100_000.0, 0.0, SIGMA, realized_sum=100_100.0 * 60, realized_count=60) == 1.0
    assert prob_settle_above(101_000.0, 100_000.0, -1.0, SIGMA, realized_sum=99_900.0 * 60, realized_count=60) == 0.0


def test_averaging_reduces_variance_vs_point_settlement():
    """Settling on a 60s average is less volatile than settling on the close:
    for an in-the-money spot, the averaged probability must be higher than
    the plain point-close probability with the same total horizon."""
    spot, strike, tau = 100_200.0, 100_000.0, 900.0
    p_avg = prob_settle_above(spot, strike, tau, SIGMA)
    point_sd = SIGMA * math.sqrt(tau)
    p_point = norm_cdf((math.log(spot / strike) - 0.5 * SIGMA**2 * tau) / point_sd)
    assert p_avg > p_point


def test_effective_variance_matches_a_plus_w_over_3():
    spot, strike, tau, w = 100_150.0, 100_000.0, 400.0, 60.0
    p = prob_settle_above(spot, strike, tau, SIGMA, avg_window_s=w)
    a = tau - w
    sd = SIGMA * math.sqrt(a + w / 3.0)
    expected = norm_cdf((math.log(spot / strike) - 0.5 * SIGMA**2 * (a + w / 2.0)) / sd)
    assert p == pytest.approx(expected, abs=1e-12)


def test_locked_in_result_inside_window():
    """With 55 of 60 ticks realized far above strike, no plausible path loses."""
    strike = 100_000.0
    realized = 55 * 100_500.0
    p = prob_settle_above(100_500.0, strike, 5.0, SIGMA, realized_sum=realized, realized_count=55)
    assert p > 0.9999


def test_k_adj_locked_returns_one():
    # Realized sum alone already exceeds 60 * strike -> mathematically locked.
    strike = 100.0
    p = prob_settle_above(50.0, strike, 10.0, SIGMA, realized_sum=6001.0, realized_count=50)
    assert p == 1.0


def test_more_realized_above_strike_raises_prob():
    strike = 100_000.0
    spot = 100_050.0
    p_few = prob_settle_above(spot, strike, 40.0, SIGMA, realized_sum=20 * 100_050.0, realized_count=20)
    p_many = prob_settle_above(spot, strike, 20.0, SIGMA, realized_sum=40 * 100_050.0, realized_count=40)
    assert p_many > p_few


def test_all_ticks_realized():
    strike = 100.0
    assert prob_settle_above(99.0, strike, 1.0, SIGMA, realized_sum=60 * 101.0, realized_count=60) == 1.0
    assert prob_settle_above(101.0, strike, 1.0, SIGMA, realized_sum=60 * 99.0, realized_count=60) == 0.0


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        prob_settle_above(0.0, 100.0, 10.0, SIGMA)
    with pytest.raises(ValueError):
        prob_settle_above(100.0, -1.0, 10.0, SIGMA)


class TestEwmaVol:
    def _feed(self, vol: EwmaVol, r: float, n: int, p0: float = 100_000.0):
        p = p0
        for i in range(n):
            p *= math.exp(r if i % 2 == 0 else -r)
            vol.update(float(i), p)

    def test_converges_to_realized(self):
        vol = EwmaVol(half_life_s=60.0, min_samples=10, floor_annual=0.0001, cap_annual=100.0)
        self._feed(vol, 1e-3, 600)
        assert vol.ready
        assert vol.sigma_s == pytest.approx(1e-3, rel=0.15)

    def test_not_ready_before_min_samples(self):
        vol = EwmaVol(min_samples=50)
        self._feed(vol, 1e-3, 20)
        assert not vol.ready

    def test_floor_and_cap(self):
        vol = EwmaVol(half_life_s=60.0, min_samples=5, floor_annual=0.5, cap_annual=1.0)
        self._feed(vol, 1e-9, 100)  # nearly flat -> clamps to floor
        assert vol.sigma_annual == pytest.approx(0.5, rel=1e-6)
        vol2 = EwmaVol(half_life_s=60.0, min_samples=5, floor_annual=0.001, cap_annual=0.10)
        self._feed(vol2, 5e-3, 100)  # wild -> clamps to cap
        assert vol2.sigma_annual == pytest.approx(0.10, rel=1e-6)

    def test_gap_resets_pairing(self):
        vol = EwmaVol(min_samples=5)
        vol.update(0.0, 100.0)
        vol.update(100.0, 200.0)  # 100s gap: ignored, no giant return recorded
        assert vol._n == 0
