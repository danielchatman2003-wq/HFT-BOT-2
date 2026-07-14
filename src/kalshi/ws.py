"""Kalshi websocket market-data + private-fill client.

Maintains local order books for subscribed markets from
orderbook_snapshot/orderbook_delta, forwards public trade prints and private
fills, and enforces sequence integrity: any per-subscription seq gap forces
a full reconnect (books are rebuilt from fresh snapshots), because a book
with a silent hole in it is worse than a brief resubscribe.

Events are pushed into an asyncio.Queue as (kind, payload) tuples:
  ("kalshi_book", ticker)      -- book for ticker changed
  ("trade", dict)              -- normalized public trade print
  ("fill", FillEvent)          -- our own execution (auth'd connections)
  ("kalshi_status", str)       -- "connected" / "disconnected"
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

from src.config import Config
from src.kalshi.auth import KalshiSigner
from src.kalshi.orderbook import KalshiBook, norm_price, parse_fill

logger = logging.getLogger("kalshi.ws")

WS_PATH = "/trade-api/ws/v2"


class KalshiWs:
    def __init__(self, cfg: Config, signer: KalshiSigner | None, events: asyncio.Queue) -> None:
        self.cfg = cfg
        self.signer = signer
        self.events = events
        self.books: dict[str, KalshiBook] = {}
        self.last_msg_ts: float = 0.0
        self.connected = False

        self._tickers: set[str] = set()
        self._stop = asyncio.Event()
        self._ws = None
        self._cmd_id = 0
        self._seq: dict[int, int] = {}          # sid -> last seq
        self._resubscribe = asyncio.Event()     # set when the ticker set changes

    # ---- public API ----

    def set_markets(self, tickers: set[str]) -> None:
        """Declare the set of market tickers to track; triggers resubscribe."""
        if tickers != self._tickers:
            self._tickers = set(tickers)
            for t in tickers:
                self.books.setdefault(t, KalshiBook())
            self._resubscribe.set()

    def age_ms(self, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        if self.last_msg_ts == 0.0:
            return float("inf")
        return (now - self.last_msg_ts) * 1000.0

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            connected_at = 0.0
            try:
                async with self._connect() as ws:
                    self._ws = ws
                    connected_at = time.time()
                    self.connected = True
                    self.last_msg_ts = time.time()
                    await self._push(("kalshi_status", "connected"))
                    self._seq.clear()
                    await self._subscribe_all(ws)
                    watchdog = asyncio.create_task(self._watchdog(ws))
                    resub = asyncio.create_task(self._resubscribe_loop(ws))
                    try:
                        async for raw in ws:
                            self.last_msg_ts = time.time()
                            if self._stop.is_set():
                                break
                            await self._handle(json.loads(raw))
                    finally:
                        watchdog.cancel()
                        resub.cancel()
            except asyncio.CancelledError:
                raise
            except _SeqGap as gap:
                logger.warning("sequence gap on sid %s; reconnecting for a clean book", gap.sid)
            except Exception as exc:
                logger.warning("kalshi ws error: %s", exc)
            finally:
                self.connected = False
                self._ws = None
                await self._push(("kalshi_status", "disconnected"))
            if self._stop.is_set():
                break
            if connected_at and time.time() - connected_at > 60.0:
                backoff = 1.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 15.0)

    # ---- internals ----

    def _connect(self):
        url = f"{self.cfg.kalshi_ws_host}{WS_PATH}"
        headers = self.signer.headers("GET", WS_PATH) if self.signer else {}
        kwargs = dict(ping_interval=10, ping_timeout=10, open_timeout=15, max_queue=1024)
        try:
            return websockets.connect(url, additional_headers=headers, **kwargs)
        except TypeError:  # websockets < 13 uses extra_headers
            return websockets.connect(url, extra_headers=headers, **kwargs)

    def _next_id(self) -> int:
        self._cmd_id += 1
        return self._cmd_id

    async def _subscribe_all(self, ws) -> None:
        if self._tickers:
            await ws.send(
                json.dumps(
                    {
                        "id": self._next_id(),
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["orderbook_delta", "ticker", "trade"],
                            "market_tickers": sorted(self._tickers),
                        },
                    }
                )
            )
        if self.signer is not None:
            await ws.send(
                json.dumps({"id": self._next_id(), "cmd": "subscribe", "params": {"channels": ["fill"]}})
            )
        self._resubscribe.clear()

    async def _resubscribe_loop(self, ws) -> None:
        """The tracked market set changes every 15-minute roll. Simplest
        correct behavior: close the socket; run() reconnects and subscribes
        to the new set, rebuilding books from snapshots."""
        while True:
            await self._resubscribe.wait()
            self._resubscribe.clear()
            logger.info("market set changed; recycling websocket")
            await ws.close()
            return

    async def _watchdog(self, ws) -> None:
        while True:
            await asyncio.sleep(2.0)
            if (time.time() - self.last_msg_ts) * 1000.0 > self.cfg.kalshi_ws_stale_ms * 3:
                logger.warning("kalshi ws silent too long; recycling")
                await ws.close()
                return

    async def _handle(self, msg: dict) -> None:
        mtype = msg.get("type")
        sid = msg.get("sid")
        seq = msg.get("seq")
        if sid is not None and seq is not None and mtype in ("orderbook_snapshot", "orderbook_delta"):
            last = self._seq.get(sid)
            if mtype == "orderbook_snapshot":
                self._seq[sid] = seq
            else:
                if last is not None and seq != last + 1:
                    raise _SeqGap(sid)
                self._seq[sid] = seq

        body = msg.get("msg", {}) or {}
        if mtype == "orderbook_snapshot":
            ticker = body.get("market_ticker", "")
            book = self.books.setdefault(ticker, KalshiBook())
            book.apply_snapshot(body)
            await self._push(("kalshi_book", ticker))
        elif mtype == "orderbook_delta":
            ticker = body.get("market_ticker", "")
            book = self.books.setdefault(ticker, KalshiBook())
            book.apply_delta(body)
            await self._push(("kalshi_book", ticker))
        elif mtype == "trade":
            trade = self._norm_trade(body)
            if trade:
                await self._push(("trade", trade))
        elif mtype == "fill":
            fill = parse_fill(body)
            if fill:
                await self._push(("fill", fill))
        elif mtype == "error":
            logger.error("kalshi ws error message: %s", msg)
        # "subscribed"/"ok"/"ticker" ack and heartbeat traffic needs no handling.

    @staticmethod
    def _norm_trade(body: dict) -> dict | None:
        ticker = body.get("market_ticker")
        if not ticker:
            return None
        try:
            price_raw = body.get("yes_price_dollars", body.get("yes_price"))
            price = norm_price(price_raw) if price_raw is not None else None
        except (ValueError, TypeError):
            price = None
        if price is None:
            return None
        count_raw = body.get("count") or body.get("count_fp") or 0
        return {
            "ticker": ticker,
            "yes_price": price,
            "count": int(float(count_raw)),
            "taker_side": (body.get("taker_side") or "").lower(),
            "ts": time.time(),
        }

    async def _push(self, event: tuple) -> None:
        try:
            self.events.put_nowait(event)
        except asyncio.QueueFull:
            # Drop-oldest keeps the strategy responsive under bursts; it
            # recomputes from current book state, so skipped notifications
            # are harmless.
            try:
                self.events.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.events.put_nowait(event)


class _SeqGap(Exception):
    def __init__(self, sid):
        super().__init__(f"seq gap on sid {sid}")
        self.sid = sid
