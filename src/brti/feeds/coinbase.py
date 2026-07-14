"""Coinbase Exchange public market data: level2_batch channel.

level2_batch is the unauthenticated variant of level2 (updates batched at
~50ms). Snapshot carries the full book; l2update carries [side, price, size]
changes where size is the NEW size at that level (0 removes).
"""
from __future__ import annotations

import json

from src.brti.feeds.base import BaseFeed


class CoinbaseFeed(BaseFeed):
    name = "coinbase"
    url = "wss://ws-feed.exchange.coinbase.com"
    product_id = "BTC-USD"

    async def subscribe(self, ws) -> None:
        await ws.send(
            json.dumps(
                {
                    "type": "subscribe",
                    "channels": [{"name": "level2_batch", "product_ids": [self.product_id]}],
                }
            )
        )

    def handle(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "snapshot" and msg.get("product_id") == self.product_id:
            bids = [(float(p), float(q)) for p, q in msg.get("bids", [])]
            asks = [(float(p), float(q)) for p, q in msg.get("asks", [])]
            self.book.replace(bids, asks)
        elif mtype == "l2update" and msg.get("product_id") == self.product_id:
            for side, price, size in msg.get("changes", []):
                book_side = "bid" if side == "buy" else "ask"
                self.book.set_level(book_side, float(price), float(size))
