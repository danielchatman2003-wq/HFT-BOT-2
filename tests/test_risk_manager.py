from src.risk_manager import RiskManager


def make_manager(**overrides) -> RiskManager:
    defaults = dict(
        bankroll_usd=1000,
        max_risk_per_trade=0.02,
        max_concurrent_positions=3,
        daily_loss_limit_usd=100,
        kelly_fraction=0.25,
    )
    defaults.update(overrides)
    return RiskManager(**defaults)


def test_no_edge_means_zero_kelly_stake():
    rm = make_manager()
    # Fair price would be 50c; market charges 50c -> zero edge.
    stake = rm.kelly_stake_fraction(model_prob=0.5, price_cents=50)
    assert stake == 0.0


def test_positive_edge_produces_positive_stake():
    rm = make_manager()
    # Model thinks 70% likely, market only charges 50c -> real edge.
    stake = rm.kelly_stake_fraction(model_prob=0.70, price_cents=50)
    assert stake > 0.0


def test_position_size_never_exceeds_hard_risk_cap():
    rm = make_manager(bankroll_usd=1000, max_risk_per_trade=0.02, kelly_fraction=1.0)
    # Full Kelly with a huge edge would want to bet a large fraction; cap should bind.
    sizing = rm.size_position(model_prob=0.95, price_cents=10)
    assert sizing["risk_usd"] <= 1000 * 0.02 + 1e-6


def test_max_concurrent_positions_blocks_new_trades():
    rm = make_manager(max_concurrent_positions=1)
    rm.record_position_opened()
    sizing = rm.size_position(model_prob=0.9, price_cents=10)
    assert sizing["contracts"] == 0
    assert "concurrent" in sizing["reason"]


def test_daily_loss_limit_blocks_new_trades():
    rm = make_manager(daily_loss_limit_usd=50)
    rm.record_position_opened()
    rm.record_position_closed(pnl_usd=-60)
    sizing = rm.size_position(model_prob=0.9, price_cents=10)
    assert sizing["contracts"] == 0
    assert "loss limit" in sizing["reason"]


def test_record_position_closed_updates_pnl_and_frees_slot():
    rm = make_manager()
    rm.record_position_opened()
    assert rm.open_positions == 1
    rm.record_position_closed(pnl_usd=25)
    assert rm.open_positions == 0
    assert rm.realized_pnl_today_usd == 25
