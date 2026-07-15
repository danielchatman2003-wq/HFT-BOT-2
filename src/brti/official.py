"""Official CF Benchmarks BRTI values, streamed by Kalshi's websocket
`cfbenchmarks_value` channel (see Kalshi's AsyncAPI spec).

Each tick carries:
  - the raw upstream CF frame (index value + source timestamp),
  - `avg_60s_data`: a trailing per-tick 60s average, and
  - `last_60s_windowed_average_15min`: present ONLY during the final minute
    before a quarter-hour close (:00/:15/:30/:45) -- the exact running
    settlement average for the 15-minute markets, over the window
    `(quarter_close - 60s, quarter_close]` (start-boundary tick excluded,
    close tick included; second-indexed counts 1..60).

This is the settlement index itself, so when this feed is fresh it
outranks the local replica estimator for both pricing and realized
settlement-window math.
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger("brti.official")


@dataclass
class _SettleWindow:
    value: float            # running average over the settlement window so far
    count: int              # ticks accumulated (1..60)
    window_start_ts_ms: int  # quarter_close_ms - 60000
    rx_ts: float            # local receive time


class OfficialBrti:
    def __init__(self, index_id: str = "BRTI", history_seconds: float = 240.0) -> None:
        self.index_id = index_id
        self.history_seconds = history_seconds
        self.last_value: float | None = None
        self.last_source_ts_ms: int | None = None
        self.last_rx_ts: float = 0.0
        self.samples: deque[tuple[float, float]] = deque()  # (source_ts_s, value)
        self._settle: _SettleWindow | None = None

    def on_message(self, body: dict, now: float | None = None) -> float | None:
        """Ingest one cfbenchmarks_value msg. Returns the tick value if it was
        a new (non-duplicate, in-order) tick for our index, else None."""
        now = now if now is not None else time.time()
        if body.get("index_id") != self.index_id:
            return None

        raw = body.get("data")
        try:
            frame = json.loads(raw) if isinstance(raw, str) else (raw or {})
            value = float(frame["value"])
            source_ts_ms = int(frame.get("time") or body.get("received_at") or 0)
        except (KeyError, TypeError, ValueError):
            logger.warning("unparseable cfbenchmarks frame: %.200r", raw)
            return None
        if source_ts_ms <= 0:
            return None
        if self.last_source_ts_ms is not None and source_ts_ms <= self.last_source_ts_ms:
            return None  # duplicate or out-of-order upstream tick

        self.last_source_ts_ms = source_ts_ms
        self.last_value = value
        self.last_rx_ts = now
        self.samples.append((source_ts_ms / 1000.0, value))
        cutoff = now - self.history_seconds
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

        w = body.get("last_60s_windowed_average_15min")
        if w:
            try:
                self._settle = _SettleWindow(
                    value=float(w["value"]),
                    count=int(w["window_size"]),
                    window_start_ts_ms=int(w["window_start_ts_ms"]),
                    rx_ts=now,
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("unparseable settlement-window data: %r", w)
        return value

    def age_ms(self, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        if self.last_rx_ts == 0.0:
            return float("inf")
        return (now - self.last_rx_ts) * 1000.0

    def window_stats(self, close_ts: float, now: float | None = None,
                     max_age_s: float = 3.0, align_tolerance_ms: float = 2000.0) -> tuple[float, int] | None:
        """(realized_sum, count) of the official settlement window for a market
        closing at close_ts, or None if we don't have a fresh, aligned window.
        The channel publishes the running AVERAGE and tick count; the model
        wants the sum, which is avg * count."""
        now = now if now is not None else time.time()
        s = self._settle
        if s is None or now - s.rx_ts > max_age_s:
            return None
        quarter_close_ms = s.window_start_ts_ms + 60_000
        if abs(quarter_close_ms - close_ts * 1000.0) > align_tolerance_ms:
            return None  # window belongs to a different quarter-hour close
        if s.count <= 0:
            return None
        return s.value * s.count, s.count
