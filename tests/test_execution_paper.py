"""Tests for paper execution fills and position accounting."""
import asyncio
import dataclasses

import pytest

from src.config import Config
from src.execution import PaperExecution, Position
from src.kalshi.orderbook import KalshiBook
from src.risk import RiskManager

CFG = dataclasses.replace(
    Config(),
    taker_fee_rate=0.07,
    maker_fee_rate=0.0,
    quote_ttl_s=10.0,
    daily_loss_limit_usd=1e9,  # keep kill switches out of accounting tests
    trade_log_path="",
)


def make_env():
    books = {"T": KalshiBook()}
    books["T"].apply_snapshot({"yes": [[40, 50], [39, 30]], "no": [[55, 25], [50, 40]]})
    # YES bids: 0.40x50, 0.39x30. Implied asks: 0.45x25, 0.50x40.
    risk = RiskManager(CFG)
    ex = PaperExecution(CFG, risk, books)
    return books, risk, ex


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class TestPositionAccounting:
    def test_long_then_partial_close(self):
        pos = Position()
        assert pos.apply_fill(10, 0.40) == 0.0
        assert pos.pos == 10 and pos.avg_price == pytest.approx(0.40)
        realized = pos.apply_fill(-4, 0.60)
        assert realized == pytest.approx(0.8)
        assert pos.pos == 6
        assert pos.settle(True) == pytest.approx(6 * 0.60)

    def test_short_accounting(self):
        pos = Position()
        pos.apply_fill(-10, 0.40)  # short at 0.40
        assert pos.collateral == pytest.approx(10 * 0.60)
        assert pos.settle(True) == pytest.approx(-6.0)  # lose 0.60 x 10

    def test_flip_through_zero(self):
        pos = Position()
        pos.apply_fill(5, 0.30)
        realized = pos.apply_fill(-8, 0.50)
        assert realized == pytest.approx(5 * 0.20)
        assert pos.pos == -3
        assert pos.avg_price == pytest.approx(0.50)


class TestPaperFills:
    def test_ioc_take_walks_book(self):
        books, risk, ex = make_env()
        order = run(ex.place("T", "buy_yes", 0.50, 30, tif="ioc", purpose="take"))
        assert order.filled == 30  # 25 @ 0.45 + 5 @ 0.50
        p = ex.position("T")
        assert p.pos == 30
        assert p.avg_price == pytest.approx((0.45 * 25 + 0.50 * 5) / 30)
        assert p.fees > 0
        assert not ex.open_orders("T")  # IOC never rests

    def test_ioc_respects_limit(self):
        books, risk, ex = make_env()
        order = run(ex.place("T", "buy_yes", 0.45, 30, tif="ioc", purpose="take"))
        assert order.filled == 25
        assert ex.position("T").pos == 25

    def test_post_only_crossing_rejected(self):
        books, risk, ex = make_env()
        assert run(ex.place("T", "buy_yes", 0.45, 10, post_only=True, purpose="bid")) is None
        assert ex.position("T").pos == 0

    def test_resting_quote_fills_on_trade_print(self):
        books, risk, ex = make_env()
        order = run(ex.place("T", "buy_yes", 0.42, 10, post_only=True, purpose="bid"))
        assert order is not None and order.remaining == 10
        ex.on_market_trade({"ticker": "T", "yes_price": 0.41, "count": 6, "taker_side": "no", "ts": 0})
        assert ex.position("T").pos == 6
        ex.on_market_trade({"ticker": "T", "yes_price": 0.43, "count": 5, "taker_side": "yes", "ts": 0})
        assert ex.position("T").pos == 6  # print above our bid: no fill

    def test_resting_quote_fills_when_book_crosses(self):
        books, risk, ex = make_env()
        run(ex.place("T", "buy_yes", 0.42, 10, post_only=True, purpose="bid"))
        books["T"].apply_snapshot({"yes": [[40, 10]], "no": [[59, 20]]})  # ask drops to 0.41
        ex.on_book_update("T")
        assert ex.position("T").pos == 10
        assert not ex.open_orders("T")

    def test_quote_ttl_expiry(self):
        books, risk, ex = make_env()
        order = run(ex.place("T", "buy_yes", 0.30, 10, post_only=True, purpose="bid"))
        order.created_ts -= 60.0
        ex.expire_stale_quotes()
        assert not ex.open_orders("T")

    def test_settlement_realizes_and_hits_risk(self):
        books, risk, ex = make_env()
        run(ex.place("T", "buy_yes", 0.50, 10, tif="ioc", purpose="take"))
        pnl_before = risk.realized_pnl_today
        ex.settle("T", result_yes=True)
        assert ex.position("T").pos == 0
        assert risk.realized_pnl_today > pnl_before  # bought ~0.46 avg, paid 1.0

    def test_cancel_all(self):
        books, risk, ex = make_env()
        run(ex.place("T", "buy_yes", 0.30, 5, post_only=True, purpose="bid"))
        run(ex.place("T", "sell_yes", 0.60, 5, post_only=True, purpose="ask"))
        assert len(ex.open_orders("T")) == 2
        run(ex.cancel_all("T"))
        assert not ex.open_orders("T")
