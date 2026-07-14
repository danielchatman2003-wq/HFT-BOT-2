"""Bitstamp public websocket: order_book_btcusd channel.

Bitstamp publishes a full top-100 snapshot on every update (~100ms), so the
book is simply replaced each message -- no diff bookkeeping to drift.
"""
from __future__ import annotations

import json

from src.brti.feeds.base import BaseFeed


class BitstampFeed(BaseFeed):
    name = "bitstamp"
    url = "wss://ws.bitstamp.net"
    channel = "order_book_btcusd"

    async def subscribe(self, ws) -> None:
        await ws.send(json.dumps({"event": "bts:subscribe", "data": {"channel": self.channel}}))

    def handle(self, msg: dict) -> None:
        if msg.get("event") != "data" or msg.get("channel") != self.channel:
            return
        data = msg.get("data", {})
        bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in data.get("asks", [])]
        if bids and asks:
            self.book.replace(bids, asks)
