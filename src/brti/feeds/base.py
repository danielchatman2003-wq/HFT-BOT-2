"""Reconnecting websocket feed base class for exchange L2 books.

Each adapter owns an L2Book, subscribes to its exchange's public BTC-USD
depth channel, and keeps the book current. The BRTI estimator drops any
feed whose book goes stale or crossed, so a dead feed degrades the index
estimate instead of poisoning it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

from src.brti.book import L2Book

logger = logging.getLogger("feeds")


class BaseFeed:
    name = "base"
    url = ""

    def __init__(self) -> None:
        self.book = L2Book()
        self.connected = False
        self._stop = asyncio.Event()

    # ---- overridables ----

    async def subscribe(self, ws) -> None:
        raise NotImplementedError

    def handle(self, msg: dict) -> None:
        raise NotImplementedError

    def on_disconnect(self) -> None:
        """Called when the connection drops; clear state that must not
        survive a reconnect (books are rebuilt from fresh snapshots)."""
        self.book.clear()

    # ---- lifecycle ----

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            connected_at = 0.0
            try:
                async with websockets.connect(
                    self.url, ping_interval=20, ping_timeout=15, max_queue=512, open_timeout=15
                ) as ws:
                    connected_at = time.time()
                    self.connected = True
                    logger.info("%s: connected", self.name)
                    await self.subscribe(ws)
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            self.handle(json.loads(raw))
                        except Exception:
                            logger.exception("%s: failed handling message", self.name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("%s: connection error: %s", self.name, exc)
            finally:
                self.connected = False
                self.on_disconnect()
            if self._stop.is_set():
                break
            # Reset backoff after a healthy stint, otherwise grow it.
            if connected_at and time.time() - connected_at > 60.0:
                backoff = 1.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)
