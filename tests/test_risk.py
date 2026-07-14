"""Tests for risk gates, sizing, and kill switches."""
import dataclasses

from src.config import Config
from src.risk import RiskManager

CFG = dataclasses.replace(
    Config(),
    bankroll_usd=1000.0,
    kelly_fraction=0.25,
    max_risk_per_trade=0.02,
    max_taker_size=20,
    max_pos_per_market=50,
    max_total_notional_usd=500.0,
    quote_size=10,
    daily_loss_limit_usd=100.0,
    max_consecutive_order_errors=3,
    brti_stale_ms=2500.0,
    kalshi_ws_stale_ms=5000.0,
)


def make_risk() -> RiskManager:
    return RiskManager(CFG)


class TestTakerSize:
    def test_capped_by_per_trade_risk(self):
        risk = make_risk()
        # fair 0.60 vs ask 0.50: kelly = 0.1/0.5 = 0.2; 0.25-kelly = $50,
        # per-trade cap = $20 -> 40 contracts at $0.50 -> max_taker_size 20.
        n = risk.taker_size(0.60, 0.50, +1, current_pos=0, collateral_in_use=0.0)
        assert n == 20

    def test_no_edge_no_trade(self):
        risk = make_risk()
        assert risk.taker_size(0.50, 0.50, +1, 0, 0.0) == 0
        assert risk.taker_size(0.40, 0.50, +1, 0, 0.0) == 0

    def test_sell_direction(self):
        risk = make_risk()
        # fair 0.40 vs bid 0.50 -> positive short edge.
        assert risk.taker_size(0.40, 0.50, -1, 0, 0.0) > 0
        assert risk.taker_size(0.60, 0.50, -1, 0, 0.0) == 0

    def test_position_room(self):
        risk = make_risk()
        assert risk.taker_size(0.90, 0.50, +1, current_pos=45, collateral_in_use=0.0) <= 5
        # From a short, buying can cross zero: room is max_pos + |short|.
        assert risk.taker_size(0.90, 0.50, +1, current_pos=-50, collateral_in_use=0.0) == 20

    def test_notional_cap(self):
        risk = make_risk()
        assert risk.taker_size(0.90, 0.50, +1, 0, collateral_in_use=499.5) <= 1
        assert risk.taker_size(0.90, 0.50, +1, 0, collateral_in_use=500.0) == 0

    def test_halted_blocks(self):
        risk = make_risk()
        risk.halt("test")
        assert risk.taker_size(0.90, 0.50, +1, 0, 0.0) == 0
        assert risk.quote_size(+1, 0, 0.0) == 0


class TestQuoteSize:
    def test_full_size_when_flat(self):
        assert make_risk().quote_size(+1, 0, 0.0) == 10

    def test_shrinks_near_cap(self):
        risk = make_risk()
        assert risk.quote_size(+1, 45, 0.0) == 5
        assert risk.quote_size(+1, 50, 0.0) == 0
        assert risk.quote_size(-1, 50, 0.0) == 10  # other side still fine


class TestKillSwitches:
    def test_daily_loss_halts(self):
        risk = make_risk()
        risk.on_realized_pnl(-50.0)
        assert not risk.halted
        risk.on_realized_pnl(-51.0)
        assert risk.halted
        assert risk.halt_reason == "daily loss limit"

    def test_consecutive_order_errors_halt(self):
        risk = make_risk()
        risk.record_order_error()
        risk.record_order_ok()
        risk.record_order_error()
        risk.record_order_error()
        assert not risk.halted
        risk.record_order_error()
        assert risk.halted

    def test_data_gate(self):
        risk = make_risk()
        assert risk.can_trade(1000.0, 1000.0, True)
        assert not risk.can_trade(9999.0, 1000.0, True)   # stale BRTI
        assert not risk.can_trade(1000.0, 99999.0, True)  # stale Kalshi
        assert not risk.can_trade(1000.0, 1000.0, False)  # vol not ready

    def test_day_roll_resets_pnl_but_not_halt(self):
        risk = make_risk()
        risk.on_realized_pnl(-150.0)
        assert risk.halted
        risk._day_key = "1999-01-01"
        risk.roll_day()
        assert risk.realized_pnl_today == 0.0
        assert risk.halted  # loss-limit halts require a human restart
