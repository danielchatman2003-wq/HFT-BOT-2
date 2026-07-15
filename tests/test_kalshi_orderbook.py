"""Tests for Kalshi book maintenance and wire-format normalization."""
import pytest

from src.kalshi.orderbook import KalshiBook, norm_price, parse_fill


class TestNormPrice:
    def test_int_is_cents(self):
        assert norm_price(96) == pytest.approx(0.96)
        assert norm_price(1) == pytest.approx(0.01)

    def test_string_is_dollars(self):
        assert norm_price("0.9600") == pytest.approx(0.96)
        assert norm_price("0.005") == pytest.approx(0.005)

    def test_float_heuristic(self):
        assert norm_price(0.96) == pytest.approx(0.96)   # dollars
        assert norm_price(96.0) == pytest.approx(0.96)   # legacy cents as float


class TestBook:
    def test_legacy_snapshot_and_bbo(self):
        book = KalshiBook()
        book.apply_snapshot({"yes": [[40, 100], [39, 50]], "no": [[55, 80], [54, 20]]})
        assert book.best_bid() == pytest.approx(0.40)
        assert book.best_ask() == pytest.approx(0.45)  # 1 - best NO bid 0.55
        assert book.mid() == pytest.approx(0.425)

    def test_dollars_fp_snapshot(self):
        book = KalshiBook()
        book.apply_snapshot(
            {"yes_dollars_fp": [["0.4000", "100.00"]], "no_dollars_fp": [["0.5500", "80.00"]]}
        )
        assert book.best_bid() == pytest.approx(0.40)
        assert book.best_ask() == pytest.approx(0.45)

    def test_delta_add_update_remove(self):
        book = KalshiBook()
        book.apply_snapshot({"yes": [[40, 100]], "no": [[55, 80]]})
        book.apply_delta({"side": "yes", "price": 41, "delta": 30})
        assert book.best_bid() == pytest.approx(0.41)
        book.apply_delta({"side": "yes", "price": 41, "delta": -30})
        assert book.best_bid() == pytest.approx(0.40)
        # dollars-fp shaped delta
        book.apply_delta({"side": "no", "price_dollars": "0.5600", "delta_fp": "10.00"})
        assert book.best_ask() == pytest.approx(0.44)

    def test_walk_buy_across_levels_and_limit(self):
        book = KalshiBook()
        book.apply_snapshot({"yes": [], "no": [[55, 10], [50, 20]]})  # asks at 0.45x10, 0.50x20
        avg, filled = book.walk("buy", 15)
        assert filled == 15
        assert avg == pytest.approx((0.45 * 10 + 0.50 * 5) / 15)
        avg, filled = book.walk("buy", 15, limit_price=0.45)
        assert filled == 10
        assert avg == pytest.approx(0.45)

    def test_walk_sell(self):
        book = KalshiBook()
        book.apply_snapshot({"yes": [[40, 5], [38, 10]], "no": []})
        avg, filled = book.walk("sell", 8)
        assert filled == 8
        assert avg == pytest.approx((0.40 * 5 + 0.38 * 3) / 8)


class TestParseFill:
    def test_legacy_buy_yes(self):
        f = parse_fill(
            {"market_ticker": "T", "order_id": "o1", "side": "yes", "action": "buy",
             "count": 5, "yes_price": 40, "no_price": 60, "is_taker": True, "ts": 123}
        )
        assert f.signed_count == 5
        assert f.price == pytest.approx(0.40)
        assert f.is_taker

    def test_legacy_buy_no_is_short_yes(self):
        f = parse_fill(
            {"market_ticker": "T", "order_id": "o1", "side": "no", "action": "buy",
             "count": 5, "yes_price": 40, "no_price": 60, "is_taker": False, "ts": 123}
        )
        assert f.signed_count == -5
        assert f.price == pytest.approx(0.40)

    def test_legacy_sell_yes(self):
        f = parse_fill(
            {"market_ticker": "T", "order_id": "o1", "side": "yes", "action": "sell",
             "count": 3, "yes_price": 42, "ts": 123}
        )
        assert f.signed_count == -3
        assert f.price == pytest.approx(0.42)

    def test_v2_bid_ask_style(self):
        f = parse_fill(
            {"market_ticker": "T", "order_id": "o2", "side": "bid", "count": "4",
             "price_dollars": "0.3100", "is_taker": False, "ts_ms": 123456}
        )
        assert f.signed_count == 4
        assert f.price == pytest.approx(0.31)
        f2 = parse_fill(
            {"market_ticker": "T", "order_id": "o3", "side": "ask", "count": 2,
             "price": "0.7000", "ts": 5}
        )
        assert f2.signed_count == -2
        assert f2.price == pytest.approx(0.70)

    def test_no_price_only_falls_back(self):
        f = parse_fill(
            {"market_ticker": "T", "side": "no", "action": "sell", "count": 2,
             "no_price": 65, "ts": 5}
        )
        assert f.signed_count == 2          # selling NO = +YES exposure
        assert f.price == pytest.approx(0.35)

    def test_spec_canonical_format(self):
        """Kalshi's AsyncAPI fill shape: book_side/outcome_side canonical,
        yes_price_dollars, count_fp, actual fee_cost, post_position_fp."""
        f = parse_fill(
            {"trade_id": "t1", "order_id": "o1", "market_ticker": "HIGHNY-22DEC23-B53.5",
             "is_taker": True, "side": "yes", "action": "buy", "outcome_side": "yes",
             "book_side": "bid", "yes_price_dollars": "0.750", "count_fp": "278.00",
             "fee_cost": "0.0300", "ts_ms": 1671899397000, "post_position_fp": "500.00"}
        )
        assert f.signed_count == 278
        assert f.price == pytest.approx(0.75)
        assert f.fee == pytest.approx(0.03)
        assert f.post_position == pytest.approx(500.0)
        assert f.ts == pytest.approx(1671899397.0)

    def test_canonical_fields_win_over_legacy(self):
        # sell-no positions the user for YES; deprecated side/action disagree.
        f = parse_fill(
            {"market_ticker": "T", "count_fp": "5.00", "outcome_side": "yes",
             "book_side": "bid", "side": "no", "action": "sell",
             "yes_price_dollars": "0.4000", "ts": 5}
        )
        assert f.signed_count == 5
        f2 = parse_fill(
            {"market_ticker": "T", "count_fp": "5.00", "outcome_side": "no",
             "yes_price_dollars": "0.4000", "ts": 5}
        )
        assert f2.signed_count == -5

    def test_fee_absent_is_none(self):
        f = parse_fill(
            {"market_ticker": "T", "side": "yes", "action": "buy", "count": 5,
             "yes_price": 40, "ts": 123}
        )
        assert f.fee is None
        assert f.post_position is None

    def test_garbage_returns_none(self):
        assert parse_fill({}) is None
        assert parse_fill({"market_ticker": "T", "count": 0}) is None


class TestUseYesPrice:
    """With use_yes_price=True, no-side wire prices arrive in YES-leg terms."""

    def test_snapshot(self):
        book = KalshiBook(use_yes_price=True)
        # NO bid at no-leg 0.55 arrives as yes-leg 0.45 when the flag is on.
        book.apply_snapshot(
            {"yes_dollars_fp": [["0.4000", "100.00"]], "no_dollars_fp": [["0.4500", "80.00"]]}
        )
        assert book.best_bid() == pytest.approx(0.40)
        assert book.best_ask() == pytest.approx(0.45)

    def test_delta(self):
        book = KalshiBook(use_yes_price=True)
        book.apply_snapshot({"yes_dollars_fp": [["0.4000", "100.00"]], "no_dollars_fp": []})
        book.apply_delta({"side": "no", "price_dollars": "0.4400", "delta_fp": "10.00"})
        assert book.best_ask() == pytest.approx(0.44)

    def test_default_mode_unchanged(self):
        book = KalshiBook()
        book.apply_snapshot(
            {"yes_dollars_fp": [["0.4000", "100.00"]], "no_dollars_fp": [["0.5500", "80.00"]]}
        )
        assert book.best_ask() == pytest.approx(0.45)
