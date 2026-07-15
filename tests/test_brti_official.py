"""Tests for the official cfbenchmarks_value BRTI feed and source selection."""
import json

import pytest

from src.brti.index import BrtiEstimator
from src.brti.official import OfficialBrti
from src.brti.source import IndexSource
from src.pricing import EwmaVol


def make_msg(value: float, ts_ms: int, index_id: str = "BRTI", settle: dict | None = None) -> dict:
    body = {
        "index_id": index_id,
        "received_at": ts_ms,
        "data": json.dumps({"type": "value", "id": index_id, "time": ts_ms, "value": f"{value:.2f}"}),
        "avg_60s_data": {
            "value": f"{value:.8f}", "window_size": 3,
            "window_start_ts_ms": ts_ms - 60000, "window_end_ts_exclusive": ts_ms,
        },
    }
    if settle:
        body["last_60s_windowed_average_15min"] = settle
    return body


class TestOfficialBrti:
    def test_parses_raw_frame(self):
        off = OfficialBrti()
        v = off.on_message(make_msg(68000.12, 1_710_000_000_123), now=100.0)
        assert v == pytest.approx(68000.12)
        assert off.last_value == pytest.approx(68000.12)
        assert off.age_ms(now=100.5) == pytest.approx(500.0)

    def test_dedupes_by_source_ts(self):
        off = OfficialBrti()
        assert off.on_message(make_msg(68000.0, 1000), now=1.0) is not None
        assert off.on_message(make_msg(68001.0, 1000), now=2.0) is None   # duplicate ts
        assert off.on_message(make_msg(68001.0, 900), now=3.0) is None    # out of order
        assert off.on_message(make_msg(68002.0, 2000), now=4.0) is not None

    def test_ignores_other_indices(self):
        off = OfficialBrti(index_id="BRTI")
        assert off.on_message(make_msg(3500.0, 1000, index_id="ETHUSD_RTI"), now=1.0) is None

    def test_settlement_window_aligned(self):
        off = OfficialBrti()
        close_ts = 1_710_000_900.0  # quarter-hour close, seconds
        settle = {
            "value": "68000.50000000", "window_size": 14,
            "window_start_ts_ms": int(close_ts * 1000) - 60000,
            "window_end_ts_exclusive": int(close_ts * 1000) - 46000,
        }
        off.on_message(make_msg(68000.5, int(close_ts * 1000) - 46000, settle=settle), now=50.0)
        stats = off.window_stats(close_ts, now=51.0)
        assert stats is not None
        realized_sum, count = stats
        assert count == 14
        assert realized_sum == pytest.approx(68000.5 * 14)

    def test_settlement_window_misaligned_or_stale(self):
        off = OfficialBrti()
        close_ts = 1_710_000_900.0
        settle = {
            "value": "68000.5", "window_size": 14,
            "window_start_ts_ms": int(close_ts * 1000) - 60000,
            "window_end_ts_exclusive": 0,
        }
        off.on_message(make_msg(68000.5, 1000, settle=settle), now=50.0)
        assert off.window_stats(close_ts + 900.0, now=51.0) is None  # different quarter
        assert off.window_stats(close_ts, now=60.0) is None          # stale (>3s)


def make_source(mode: str = "auto"):
    replica = BrtiEstimator()
    official = OfficialBrti()
    vol = EwmaVol(min_samples=1)
    return IndexSource(mode, replica, official, vol, stale_ms=2500.0), replica, official, vol


class TestIndexSource:
    def test_prefers_official_when_fresh(self):
        import time
        src, replica, official, vol = make_source("auto")
        now = time.time()
        replica.last_value, replica.last_ts = 67990.0, now
        src.on_official(make_msg(68000.0, int(now * 1000)), now=now)
        assert src.last_value == pytest.approx(68000.0)
        assert src.active_source() == "official"
        assert src.divergence() == pytest.approx(10.0)

    def test_falls_back_to_replica_when_official_stale(self):
        import time
        src, replica, official, vol = make_source("auto")
        now = time.time()
        src.on_official(make_msg(68000.0, int((now - 60) * 1000)), now=now - 60)
        replica.last_value, replica.last_ts = 67990.0, now
        assert src.last_value == pytest.approx(67990.0)
        assert src.active_source() == "replica"
        assert src.divergence() is None  # official stale -> not comparable

    def test_replica_mode_ignores_official(self):
        import time
        src, replica, official, vol = make_source("replica")
        now = time.time()
        src.on_official(make_msg(68000.0, int(now * 1000)), now=now)
        replica.last_value, replica.last_ts = 67990.0, now
        assert src.last_value == pytest.approx(67990.0)

    def test_vol_not_double_fed(self):
        import time
        src, replica, official, vol = make_source("auto")
        now = time.time()
        src.on_official(make_msg(68000.0, int(now * 1000)), now=now)
        n_after_official = vol._n + (1 if vol._last_price is not None else 0)
        # Replica sample while official is fresh must NOT feed vol.
        src.on_replica_sample(now + 0.01, 67990.0)
        assert (vol._n + (1 if vol._last_price is not None else 0)) == n_after_official

    def test_window_stats_prefers_official(self):
        import time
        src, replica, official, vol = make_source("auto")
        now = time.time()
        close_ts = now + 30.0
        replica.samples.extend((close_ts - 60 + i, 100.0) for i in range(1, 11))
        settle = {
            "value": "200.0", "window_size": 5,
            "window_start_ts_ms": int(close_ts * 1000) - 60000,
            "window_end_ts_exclusive": int(now * 1000),
        }
        src.on_official(make_msg(200.0, int(now * 1000), settle=settle), now=now)
        total, count = src.window_stats(close_ts, 60.0, now=now)
        assert count == 5 and total == pytest.approx(1000.0)
        # Without a fresh official window, replica stats flow through.
        src2, replica2, official2, vol2 = make_source("auto")
        replica2.samples.extend((close_ts - 60 + i, 100.0) for i in range(1, 11))
        total2, count2 = src2.window_stats(close_ts, 60.0, now=now)
        assert count2 == 10 and total2 == pytest.approx(1000.0)
