from src.fees import fee_per_contract, order_fee


def test_published_example_at_50c():
    # Kalshi's own example: 0.07 * 100 * 0.5 * 0.5 = $1.75 exactly.
    assert order_fee(0.50, 100, 0.07) == 1.75


def test_rounds_up_to_cent():
    # 0.07 * 1 * 0.35 * 0.65 = 0.0159... -> $0.02
    assert order_fee(0.35, 1, 0.07) == 0.02
    # 0.10 * 1 * 0.5 * 0.5 = 0.025 -> exactly 2.5c -> ceil -> $0.03
    assert order_fee(0.50, 1, 0.10) == 0.03


def test_zero_count():
    assert order_fee(0.50, 0, 0.07) == 0.0


def test_extremes_cost_less_than_mid():
    assert order_fee(0.05, 100, 0.10) < order_fee(0.50, 100, 0.10)
    assert order_fee(0.95, 100, 0.10) < order_fee(0.50, 100, 0.10)


def test_fee_per_contract_single_is_conservative():
    bulk = order_fee(0.35, 100, 0.07) / 100
    single = fee_per_contract(0.35, 0.07)
    assert single >= bulk
