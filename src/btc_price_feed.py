"""Live BTC-USD price feed via Coinbase's public websocket, with a rolling
realized-volatility estimate used by the pricing model.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import deque

import websocket

from src.config import CONFIG


class BTCPriceFeed:
    """Maintains the latest BTC-USD trade price and a rolling window of
    (timestamp, price) samples used to estimate short-horizon volatility.
    """

    def __init__(self, window_seconds: int = 3600, product_id: str = "BTC-USD"):
        self.product_id = product_id
        self.window_seconds = window_seconds
        self._samples: deque[tuple[float, float]] = deque()
        self._lock = threading.Lock()
        self._latest_price: float | None = None
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---- public API ----

    def start(self):
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._ws:
            self._ws.close()

    def latest_price(self) -> float | None:
        with self._lock:
            return self._latest_price

    def realized_vol_annualized(self, lookback_seconds: int | None = None) -> float | None:
        """Annualized realized volatility from log returns of recent samples."""
        with self._lock:
            samples = list(self._samples)
        if lookback_seconds:
            cutoff = time.time() - lookback_seconds
            samples = [s for s in samples if s[0] >= cutoff]
        if len(samples) < 10:
            return None

        log_returns = []
        for (t0, p0), (t1, p1) in zip(samples, samples[1:]):
            dt = t1 - t0
            if dt <= 0 or p0 <= 0 or p1 <= 0:
                continue
            log_returns.append(math.log(p1 / p0) / math.sqrt(dt))
        if len(log_returns) < 5:
            return None

        mean = sum(log_returns) / len(log_returns)
        variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
        # log_returns are already per-sqrt-second; scale std dev to annualized.
        seconds_per_year = 365 * 24 * 3600
        return math.sqrt(variance) * math.sqrt(seconds_per_year)

    # ---- internals ----

    def _on_message(self, _ws, message: str):
        data = json.loads(message)
        if data.get("type") != "ticker":
            return
        price = data.get("price")
        if price is None:
            return
        now = time.time()
        with self._lock:
            self._latest_price = float(price)
            self._samples.append((now, float(price)))
            cutoff = now - self.window_seconds
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()

    def _on_open(self, ws):
        sub = {
            "type": "subscribe",
            "product_ids": [self.product_id],
            "channels": ["ticker"],
        }
        ws.send(json.dumps(sub))

    def _run_forever(self):
        backoff = 1
        while not self._stop.is_set():
            try:
                self._ws = websocket.WebSocketApp(
                    CONFIG.coinbase_ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                )
                backoff = 1
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception:
                pass
            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
