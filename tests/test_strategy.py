"""Strategy decision tests: taking, quoting, and safety gates, run against
paper execution with a hand-built Kalshi book and a stubbed estimator."""
import asyncio
import dataclasses
import time

import pytest

from src.brti.index import BrtiEstimator
from src.config import Config
from src.execution import PaperExecution
from src.kalshi.orderbook import KalshiBook
from src.pricing import EwmaVol, annual_to_per_sqrt_sec
from src.risk import RiskManager
from src.strategy import UpDownStrategy

CFG = dataclasses.replace(
    Config(),
    taker_fee_rate=0.07,
    maker_fee_rate=0.0,
    min_taker_edge_cents=3.0,
    min_maker_edge_cents=1.0,
    quote_size=10,
    max_taker_size=20,
    maker_stop_before_close_s=12.0,
    taker_stop_before_close_s=1.5,
    min_replace_interval_ms=0.0,
    daily_loss_limit_usd=1e9,
    trade_log_path="",
)


class StubVol(EwmaVol):
    def __init__(self, sigma_annual: float = 0.60):
        super().__init__(min_samples=0, floor_annual=0.0001, cap_annual=100.0)
        self._sigma = annual_to_per_sqrt_sec(sigma_annual)
        self._n = 999

    @property
    def ready(self) -> bool:  # type: ignore[override]
        return True

    @property
    def sigma_s(self) -> float:  # type: ignore[override]
        return self._sigma


def make_world(spot: float, book_yes_bid: int, book_no_bid: int, close_in_s: float = 600.0, strike: float = 100_000.0):
    now = time.time()
    est = BrtiEstimator()
    est.last_value = spot
    est.last_ts = now
    vol = StubVol()
    books = {"T": KalshiBook()}
    books["T"].apply_snapshot({"yes": [[book_yes_bid, 100]], "no": [[book_no_bid, 100]]})
    risk = RiskManager(CFG)
    ex = PaperExecution(CFG, risk, books)
    strat = UpDownStrategy(CFG, est, vol, books, ex, risk)
    strat.upsert_market("T", now + close_in_s, strike)
    return strat, ex, est, books, now


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_takes_cheap_ask_when_deep_itm():
    # Spot far above strike -> fair ~1, but the ask is 55c: huge buy edge.
    strat, ex, est, books, now = make_world(spot=100_500.0, book_yes_bid=40, book_no_bid=45)
    run(strat.evaluate(strat.markets["T"], now))
    assert ex.position("T").pos > 0
    assert strat.markets["T"].takes == 1


def test_takes_rich_bid_when_deep_otm():
    # Spot far below strike -> fair ~0, bid is 60c: sell it.
    strat, ex, est, books, now = make_world(spot=99_500.0, book_yes_bid=60, book_no_bid=35)
    run(strat.evaluate(strat.markets["T"], now))
    assert ex.position("T").pos < 0


def test_no_take_without_edge_quotes_instead():
    # Fair ~0.5 (ATM), market 48/52: no taker edge; expect two resting quotes.
    strat, ex, est, books, now = make_world(spot=100_000.0, book_yes_bid=48, book_no_bid=48)
    run(strat.evaluate(strat.markets["T"], now))
    assert ex.position("T").pos == 0
    bids = ex.open_orders("T", purpose="bid")
    asks = ex.open_orders("T", purpose="ask")
    assert len(bids) == 1 and len(asks) == 1
    assert bids[0].price < asks[0].price
    fair = strat.markets["T"].last_fair
    assert bids[0].price <= fair - CFG.min_maker_edge
    assert asks[0].price >= fair + CFG.min_maker_edge


def test_quotes_pulled_near_close():
    strat, ex, est, books, now = make_world(
        spot=100_000.0, book_yes_bid=48, book_no_bid=48, close_in_s=10.0
    )
    run(strat.evaluate(strat.markets["T"], now))
    assert not ex.open_orders("T", purpose="bid")
    assert not ex.open_orders("T", purpose="ask")


def test_no_quotes_at_extreme_prob_but_taker_still_allowed():
    # Spot 1% above strike with 10 minutes left: fair > 0.9999.
    strat, ex, est, books, now = make_world(spot=101_000.0, book_yes_bid=90, book_no_bid=8)
    # fair ~ 1 -> beyond EXTREME_PROB_NO_QUOTE: no quotes...
    run(strat.evaluate(strat.markets["T"], now))
    assert not ex.open_orders("T", purpose="bid")
    assert not ex.open_orders("T", purpose="ask")
    # ...but the 92c ask vs ~1.00 fair take is allowed.
    assert ex.position("T").pos > 0


def test_stale_brti_blocks_everything():
    strat, ex, est, books, now = make_world(spot=100_500.0, book_yes_bid=40, book_no_bid=45)
    est.last_ts = now - 60.0  # very stale index
    run(strat.evaluate(strat.markets["T"], now))
    assert ex.position("T").pos == 0
    assert not ex.open_orders("T")


def test_halted_risk_blocks_and_pulls_quotes():
    strat, ex, est, books, now = make_world(spot=100_000.0, book_yes_bid=48, book_no_bid=48)
    run(strat.evaluate(strat.markets["T"], now))
    assert ex.open_orders("T")
    ex.risk.halt("test")
    run(strat.evaluate(strat.markets["T"], now))
    assert not ex.open_orders("T")
    assert ex.position("T").pos == 0


def test_missing_strike_means_no_action():
    strat, ex, est, books, now = make_world(spot=100_500.0, book_yes_bid=40, book_no_bid=45)
    strat.markets["T"].strike = None
    run(strat.evaluate(strat.markets["T"], now))
    assert ex.position("T").pos == 0
    assert not ex.open_orders("T")


def test_inventory_skew_shifts_quotes_down_when_long():
    strat, ex, est, books, now = make_world(spot=100_000.0, book_yes_bid=48, book_no_bid=48)
    run(strat.evaluate(strat.markets["T"], now))
    flat_bid = ex.open_orders("T", purpose="bid")[0].price
    flat_ask = ex.open_orders("T", purpose="ask")[0].price

    strat2, ex2, est2, books2, now2 = make_world(spot=100_000.0, book_yes_bid=48, book_no_bid=48)
    ex2.position("T").apply_fill(40, 0.50)  # heavily long
    run(strat2.evaluate(strat2.markets["T"], now2))
    long_bids = ex2.open_orders("T", purpose="bid")
    long_asks = ex2.open_orders("T", purpose="ask")
    assert long_asks and long_asks[0].price <= flat_ask
    if long_bids:  # bid may shrink or vanish as inventory approaches the cap
        assert long_bids[0].price <= flat_bid


def test_settle_market_realizes_and_stops():
    strat, ex, est, books, now = make_world(spot=100_500.0, book_yes_bid=40, book_no_bid=45)
    run(strat.evaluate(strat.markets["T"], now))
    assert ex.position("T").pos > 0
    run(strat.settle_market("T", result_yes=True))
    assert ex.position("T").pos == 0
    assert strat.markets["T"].settled
    pnl = ex.total_realized()
    assert pnl > 0  # bought ~55c, settled at $1


def test_fair_uses_realized_ticks_inside_window():
    """55 of 60 settlement ticks locked in above strike must produce a fair
    near 1 even with spot back at the strike."""
    strat, ex, est, books, now = make_world(
        spot=100_000.0, book_yes_bid=48, book_no_bid=48, close_in_s=5.0
    )
    close_ts = strat.markets["T"].close_ts
    est.samples.extend((close_ts - 60.0 + i, 100_400.0) for i in range(55))
    run(strat.evaluate(strat.markets["T"], now))
    assert strat.markets["T"].last_fair == pytest.approx(1.0, abs=1e-6)
    # And with a 48c bid vs fair ~1... the taker should have SOLD nothing;
    # buying the 52c ask is the trade (fair-ask edge ~48c).
    assert ex.position("T").pos > 0
