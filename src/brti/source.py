"""Index source selection: official Kalshi-streamed BRTI vs local replica.

The strategy consumes one duck-typed interface (last_value / age_ms /
window_stats). This facade routes it:

  BRTI_SOURCE=auto      official when fresh, replica as fallback (default)
  BRTI_SOURCE=official  official only (trading halts if the stream stalls)
  BRTI_SOURCE=replica   local estimator only

The official feed is the settlement index itself, so it wins whenever it is
fresh; the replica keeps running regardless -- it warms the vol estimator
before the websocket is up, absorbs official-stream gaps, and its divergence
from the official print is continuously measurable (a large divergence means
the replica -- or a constituent feed -- is sick, and is worth alerting on).

Volatility updates are fed from whichever source is currently active, never
both for the same tick, so the EWMA doesn't double-count.
"""
from __future__ import annotations

import time

from src.brti.index import BrtiEstimator
from src.brti.official import OfficialBrti
from src.pricing import EwmaVol


class IndexSource:
    def __init__(self, mode: str, replica: BrtiEstimator, official: OfficialBrti,
                 vol: EwmaVol, stale_ms: float = 2500.0) -> None:
        if mode not in ("auto", "official", "replica"):
            raise ValueError(f"bad BRTI_SOURCE {mode!r}")
        self.mode = mode
        self.replica = replica
        self.official = official
        self.vol = vol
        self.stale_ms = stale_ms

    # ---- ingestion ----

    def on_official(self, body: dict, now: float | None = None) -> float | None:
        now = now if now is not None else time.time()
        value = self.official.on_message(body, now)
        if value is not None and self.mode != "replica":
            self.vol.update(self.official.last_source_ts_ms / 1000.0, value)
        return value

    def on_replica_sample(self, ts: float, value: float) -> None:
        if not self._official_active(ts):
            self.vol.update(ts, value)

    # ---- strategy-facing interface (same shape as BrtiEstimator) ----

    @property
    def last_value(self) -> float | None:
        if self._official_active(time.time()):
            return self.official.last_value
        if self.mode == "official":
            return self.official.last_value  # stale; age_ms gates trading
        return self.replica.last_value

    def age_ms(self, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        if self._official_active(now) or self.mode == "official":
            return self.official.age_ms(now)
        return self.replica.age_ms(now)

    def window_stats(self, close_ts: float, window_s: float = 60.0,
                     now: float | None = None) -> tuple[float, int]:
        now = now if now is not None else time.time()
        if self.mode != "replica":
            official = self.official.window_stats(close_ts, now)
            if official is not None:
                return official
        if self.mode == "official":
            return 0.0, 0  # strategy treats count==0 as "no realized data"
        return self.replica.window_stats(close_ts, window_s, now)

    # ---- observability ----

    def active_source(self, now: float | None = None) -> str:
        return "official" if self._official_active(now if now is not None else time.time()) else (
            "official-stale" if self.mode == "official" else "replica"
        )

    def divergence(self, now: float | None = None) -> float | None:
        """official - replica, when both are fresh. Large values mean the
        replica (or one of its feeds) is sick."""
        now = now if now is not None else time.time()
        if (
            self.official.last_value is not None
            and self.replica.last_value is not None
            and self.official.age_ms(now) <= self.stale_ms
            and self.replica.age_ms(now) <= self.stale_ms
        ):
            return self.official.last_value - self.replica.last_value
        return None

    def _official_active(self, now: float) -> bool:
        if self.mode == "replica":
            return False
        return self.official.age_ms(now) <= self.stale_ms
