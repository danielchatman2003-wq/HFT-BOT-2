"""Gemini market data websocket v2: l2 channel.

The first l2_updates message after subscribing carries the full book; later
ones carry diffs. Both use changes: [side, price, qty] with qty as the new
level size (0 removes), so one handler covers both (the book starts empty).
"""
from __future__ import annotations

import json

from src.brti.feeds.base import BaseFeed


class GeminiFeed(BaseFeed):
    name = "gemini"
    url = "wss://api.gemini.com/v2/marketdata"
    symbol = "BTCUSD"

    async def subscribe(self, ws) -> None:
        await ws.send(
            json.dumps({"type": "subscribe", "subscriptions": [{"name": "l2", "symbols": [self.symbol]}]})
        )

    def handle(self, msg: dict) -> None:
        if msg.get("type") != "l2_updates" or msg.get("symbol") != self.symbol:
            return
        for side, price, qty in msg.get("changes", []):
            book_side = "bid" if side == "buy" else "ask"
            self.book.set_level(book_side, float(price), float(qty))
