from src.pricing_model import fair_value_above_strike, fair_value_between


def test_at_the_money_is_roughly_fifty_fifty():
    fv = fair_value_above_strike(spot=100_000, strike=100_000, seconds_to_expiry=900, annualized_vol=0.6)
    assert abs(fv.prob_yes - 0.5) < 0.02


def test_deep_in_the_money_is_near_certain():
    fv = fair_value_above_strike(spot=110_000, strike=90_000, seconds_to_expiry=900, annualized_vol=0.6)
    assert fv.prob_yes > 0.99


def test_deep_out_of_the_money_is_near_zero():
    fv = fair_value_above_strike(spot=90_000, strike=110_000, seconds_to_expiry=900, annualized_vol=0.6)
    assert fv.prob_yes < 0.01


def test_higher_vol_increases_uncertainty_away_from_strike():
    low_vol = fair_value_above_strike(spot=101_000, strike=100_000, seconds_to_expiry=900, annualized_vol=0.2)
    high_vol = fair_value_above_strike(spot=101_000, strike=100_000, seconds_to_expiry=900, annualized_vol=1.5)
    # Spot is above strike, so higher vol should pull prob_yes back down toward 0.5.
    assert high_vol.prob_yes < low_vol.prob_yes


def test_zero_time_to_expiry_resolves_deterministically():
    above = fair_value_above_strike(spot=101_000, strike=100_000, seconds_to_expiry=0, annualized_vol=0.6)
    below = fair_value_above_strike(spot=99_000, strike=100_000, seconds_to_expiry=0, annualized_vol=0.6)
    assert above.prob_yes == 1.0
    assert below.prob_yes == 0.0


def test_prob_yes_and_no_sum_to_one():
    fv = fair_value_above_strike(spot=100_500, strike=100_000, seconds_to_expiry=900, annualized_vol=0.6)
    assert abs(fv.prob_yes + fv.prob_no - 1.0) < 1e-9


def test_range_probability_is_bounded_and_nonnegative():
    fv = fair_value_between(spot=100_000, lower=99_000, upper=101_000, seconds_to_expiry=900, annualized_vol=0.6)
    assert 0.0 <= fv.prob_yes <= 1.0
