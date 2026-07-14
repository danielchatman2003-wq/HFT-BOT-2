"""Kraken websocket API v2: public `book` channel (top-N depth).

Snapshot and update messages share a shape: data[0] has bids/asks as
{price, qty}; qty 0 removes a level. Depth-25 is plenty for the utilized
portion of the BRTI curve (weight decays as e^(-10.3 x)). The book checksum
is not verified; a drifted book surfaces as a crossed/stale feed and gets
dropped by the estimator, then rebuilt on reconnect.
"""
from __future__ import annotations

import json

from src.brti.feeds.base import BaseFeed


class KrakenFeed(BaseFeed):
    name = "kraken"
    url = "wss://ws.kraken.com/v2"
    symbol = "BTC/USD"
    depth = 25

    async def subscribe(self, ws) -> None:
        await ws.send(
            json.dumps(
                {
                    "method": "subscribe",
                    "params": {"channel": "book", "symbol": [self.symbol], "depth": self.depth},
                }
            )
        )

    def handle(self, msg: dict) -> None:
        if msg.get("channel") != "book":
            return
        mtype = msg.get("type")
        for item in msg.get("data", []):
            if item.get("symbol") != self.symbol:
                continue
            if mtype == "snapshot":
                bids = [(float(l["price"]), float(l["qty"])) for l in item.get("bids", [])]
                asks = [(float(l["price"]), float(l["qty"])) for l in item.get("asks", [])]
                self.book.replace(bids, asks)
            elif mtype == "update":
                for l in item.get("bids", []):
                    self.book.set_level("bid", float(l["price"]), float(l["qty"]))
                for l in item.get("asks", []):
                    self.book.set_level("ask", float(l["price"]), float(l["qty"]))
