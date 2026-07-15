"""Tests for the CF-methodology BRTI estimator."""
import pytest

from src.brti.book import L2Book
from src.brti.index import BrtiEstimator


def make_estimator(**kwargs) -> BrtiEstimator:
    defaults = dict(lambda_=10.3, deviation_cap=0.005, min_depth=1.0, feed_stale_ms=5000.0)
    defaults.update(kwargs)
    return BrtiEstimator(**defaults)


def symmetric_book(mid: float, spread: float, levels: int = 10, qty: float = 2.0, step: float = 1.0) -> L2Book:
    book = L2Book()
    bids = [(mid - spread / 2 - i * step, qty) for i in range(levels)]
    asks = [(mid + spread / 2 + i * step, qty) for i in range(levels)]
    book.replace(bids, asks, ts=1000.0)
    return book


def test_symmetric_book_gives_mid():
    est = make_estimator()
    est.register("x", symmetric_book(100_000.0, 10.0))
    value = est.compute(now=1000.5)
    assert value == pytest.approx(100_000.0, abs=0.5)


def test_multiple_books_consolidate():
    est = make_estimator()
    est.register("a", symmetric_book(100_000.0, 10.0))
    est.register("b", symmetric_book(100_010.0, 10.0))
    value = est.compute(now=1000.5)
    assert 100_000.0 < value < 100_010.0


def _wall_book(with_wall: bool) -> L2Book:
    """Deep bids; thin near-touch asks plus (optionally) a big ask wall at
    +0.4% -- inside the 0.5% deviation cap, so inside utilized depth."""
    book = L2Book()
    bids = [(99_995.0 - i, 1.0) for i in range(10)]
    asks = [(100_005.0, 1.0), (100_006.0, 1.0)]
    if with_wall:
        asks.append((100_400.0, 8.0))
    book.replace(bids, asks, ts=1000.0)
    return book


def test_near_touch_weighting_dominates():
    """Liquidity far from the touch (but inside utilized depth) raises the
    cost-to-execute mid a little -- but exp(-10.3 x) weighting must shrink
    its influence to a small fraction of what uniform weighting would give."""
    clean_est = make_estimator()
    clean_est.register("x", _wall_book(with_wall=False))
    clean = clean_est.compute(now=1000.5)

    exp_est = make_estimator()
    exp_est.register("x", _wall_book(with_wall=True))
    exp_value = exp_est.compute(now=1000.5)

    uniform_est = make_estimator(lambda_=1e-3)  # ~uniform weighting
    uniform_est.register("x", _wall_book(with_wall=True))
    uniform_value = uniform_est.compute(now=1000.5)

    assert exp_value > clean            # the wall raises the weighted mid...
    assert uniform_value - clean > 10.0  # ...a lot, under uniform weighting...
    # ...but near-touch exponential weighting keeps it to a small fraction.
    assert (exp_value - clean) < 0.35 * (uniform_value - clean)


def test_far_liquidity_beyond_deviation_cap_excluded():
    """Levels past the 0.5% half-spread cap must not affect the index."""
    est = make_estimator()
    base = symmetric_book(100_000.0, 10.0, levels=5, qty=1.0)
    est.register("x", base)
    clean = est.compute(now=1000.5)

    est2 = make_estimator()
    dirty = symmetric_book(100_000.0, 10.0, levels=5, qty=1.0)
    dirty.set_level("ask", 101_000.0, 1000.0, ts=1000.0)  # 1% away: outside cap
    est2.register("x", dirty)
    assert est2.compute(now=1000.5) == pytest.approx(clean, abs=1.0)


def test_stale_feed_dropped():
    est = make_estimator(feed_stale_ms=1000.0)
    fresh = symmetric_book(100_000.0, 10.0)
    stale = symmetric_book(90_000.0, 10.0)
    stale.last_update = 900.0  # 100s old at now=1000.5
    est.register("fresh", fresh)
    est.register("stale", stale)
    assert est.compute(now=1000.5) == pytest.approx(100_000.0, abs=0.5)
    assert est.live_feeds(now=1000.5) == ["fresh"]


def test_crossed_feed_dropped():
    est = make_estimator()
    crossed = L2Book()
    crossed.replace([(100_100.0, 1.0)], [(100_000.0, 1.0)], ts=1000.0)
    est.register("crossed", crossed)
    assert est.compute(now=1000.5) is None


def test_no_feeds_returns_none():
    est = make_estimator()
    assert est.compute(now=1000.0) is None


def test_sample_and_window_stats():
    est = make_estimator()
    est.register("x", symmetric_book(100_000.0, 10.0))
    close_ts = 2000.0
    # Ticks at 1950..1959 (inside window) and 1930 (before window).
    est.samples.extend([(1930.0, 99_990.0)] + [(1950.0 + i, 100_000.0 + i) for i in range(10)])
    total, count = est.window_stats(close_ts, window_s=60.0, now=1959.5)
    assert count == 10
    assert total == pytest.approx(sum(100_000.0 + i for i in range(10)))


def test_window_stats_boundaries_match_kalshi_semantics():
    """Kalshi's settlement window is (close-60, close]: the tick exactly at
    the start boundary is EXCLUDED, the close tick is INCLUDED."""
    est = make_estimator()
    close_ts = 2000.0
    est.samples.extend([
        (1940.0, 111.0),   # exactly at close-60: excluded
        (1941.0, 100.0),   # first included tick
        (2000.0, 200.0),   # close tick: included
    ])
    total, count = est.window_stats(close_ts, window_s=60.0, now=2000.5)
    assert count == 2
    assert total == pytest.approx(300.0)


def test_sample_records_rounded_value():
    est = make_estimator()
    est.register("x", symmetric_book(100_000.335, 0.01))
    v = est.sample(now=1000.5)
    assert v is not None
    assert v == round(v, 2)
    assert est.last_value == v
    assert est.age_ms(now=1001.5) == pytest.approx(1000.0)
