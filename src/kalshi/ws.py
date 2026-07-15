"""Kalshi websocket client (per Kalshi's published AsyncAPI spec).

Maintains local order books for subscribed markets from
orderbook_snapshot/orderbook_delta and forwards the other streams the bot
consumes:

  cfbenchmarks_value    official CF index ticks (BRTI) + settlement averages
  market_lifecycle_v2   strike/close/price-band metadata + determination
  trade                 public prints (paper-mode maker fills)
  fill / user_orders / market_positions   private streams (live mode only)

Market rolls use `update_subscription` (add_markets/delete_markets) on the
existing subscriptions instead of recycling the socket. Sequence integrity:
`seq` is tracked for every sid-bearing message (control acks consume
sequence numbers too); a gap on an orderbook_delta forces a reconnect,
because a book with a silent hole is worse than a brief resubscribe. Error
codes the spec marks terminal (10/17/25) and subscription-state errors
(7/16/26) also force a clean reconnect.

Events are pushed into an asyncio.Queue as (kind, payload) tuples:
  ("kalshi_book", ticker)        book for ticker changed
  ("trade", dict)                normalized public trade print
  ("fill", FillEvent)            our own execution (live)
  ("brti_official", dict)        raw cfbenchmarks_value msg body
  ("lifecycle", dict)            raw market_lifecycle_v2 msg body
  ("user_order", dict)           raw user_orders msg body (live)
  ("market_position", dict)      raw market_positions msg body (live)
  ("kalshi_status", str)         "connected" / "disconnected"
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
MARKET_CHANNELS = ("orderbook_delta", "ticker", "trade")
# Terminal per the spec (resubscribe required) plus subscription-state errors
# that leave us unsure what the server still has for us.
RECONNECT_ERROR_CODES = {7, 10, 16, 17, 25, 26}


class KalshiWs:
    def __init__(self, cfg: Config, signer: KalshiSigner | None, events: asyncio.Queue,
                 live: bool = False) -> None:
        self.cfg = cfg
        self.signer = signer
        self.events = events
        self.live = live
        self.books: dict[str, KalshiBook] = {}
        self.last_msg_ts: float = 0.0
        self.connected = False

        self._tickers: set[str] = set()
        self._active_tickers: set[str] = set()   # what the server currently has
        self._stop = asyncio.Event()
        self._cmd_id = 0
        self._seq: dict[int, int] = {}           # sid -> last seq (all message types)
        self._channel_sids: dict[str, int] = {}  # channel -> sid
        self._resubscribe = asyncio.Event()      # set when the ticker set changes

    # ---- public API ----

    def set_markets(self, tickers: set[str]) -> None:
        """Declare the set of market tickers to track; synced via
        update_subscription without dropping the connection."""
        if tickers != self._tickers:
            self._tickers = set(tickers)
            for t in tickers:
                self.books.setdefault(t, KalshiBook(self.cfg.kalshi_use_yes_price))
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
                    connected_at = time.time()
                    self.connected = True
                    self.last_msg_ts = time.time()
                    await self._push(("kalshi_status", "connected"))
                    await self._subscribe_all(ws)
                    helpers = [
                        asyncio.create_task(self._market_sync_loop(ws)),
                        asyncio.create_task(self._watchdog(ws)),
                    ]
                    try:
                        async for raw in ws:
                            self.last_msg_ts = time.time()
                            if self._stop.is_set():
                                break
                            await self._handle(json.loads(raw))
                    finally:
                        for h in helpers:
                            h.cancel()
            except asyncio.CancelledError:
                raise
            except _Reconnect as why:
                logger.warning("reconnecting for a clean subscription state: %s", why)
            except Exception as exc:
                logger.warning("kalshi ws error: %s", exc)
            finally:
                self.connected = False
                self._active_tickers = set()
                self._channel_sids.clear()
                await self._push(("kalshi_status", "disconnected"))
            if self._stop.is_set():
                break
            if connected_at and time.time() - connected_at > 60.0:
                backoff = 1.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 15.0)

    # ---- connection / subscription management ----

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

    async def _send(self, ws, cmd: str, params: dict) -> None:
        await ws.send(json.dumps({"id": self._next_id(), "cmd": cmd, "params": params}))

    async def _subscribe_all(self, ws) -> None:
        self._seq.clear()
        self._channel_sids.clear()
        if self._tickers:
            await self._send(ws, "subscribe", self._market_params(sorted(self._tickers)))
        self._active_tickers = set(self._tickers)
        await self._send(ws, "subscribe", {"channels": ["market_lifecycle_v2"]})
        await self._send(
            ws, "subscribe",
            {"channels": ["cfbenchmarks_value"], "index_ids": [self.cfg.brti_index_id]},
        )
        if self.live:
            await self._send(ws, "subscribe", {"channels": ["fill"]})
            await self._send(ws, "subscribe", {"channels": ["user_orders"]})
            await self._send(ws, "subscribe", {"channels": ["market_positions"]})
        self._resubscribe.clear()

    def _market_params(self, tickers: list[str]) -> dict:
        params: dict = {"channels": list(MARKET_CHANNELS), "market_tickers": tickers}
        if self.cfg.kalshi_use_yes_price:
            params["use_yes_price"] = True
        return params

    async def _market_sync_loop(self, ws) -> None:
        """Applies ticker-set changes to the live subscriptions via
        update_subscription; falls back to a fresh subscribe when the market
        channels were never subscribed (e.g. we connected before discovery)."""
        while True:
            await self._resubscribe.wait()
            self._resubscribe.clear()
            want = set(self._tickers)
            have = set(self._active_tickers)
            added, removed = want - have, have - want
            if not added and not removed:
                continue
            market_sids = [sid for ch, sid in self._channel_sids.items() if ch in MARKET_CHANNELS]
            if not market_sids:
                if want:
                    await self._send(ws, "subscribe", self._market_params(sorted(want)))
                    self._active_tickers = want
                continue
            logger.info("market roll: +%s -%s", sorted(added) or "[]", sorted(removed) or "[]")
            for sid in market_sids:
                if added:
                    await self._send(ws, "update_subscription",
                                     {"sid": sid, "market_tickers": sorted(added), "action": "add_markets"})
                if removed:
                    await self._send(ws, "update_subscription",
                                     {"sid": sid, "market_tickers": sorted(removed), "action": "delete_markets"})
            self._active_tickers = want

    async def _watchdog(self, ws) -> None:
        # The cfbenchmarks_value stream ticks ~1/sec, so a healthy connection
        # is never silent for long (server pings are handled by the library).
        threshold_ms = max(30_000.0, self.cfg.kalshi_ws_stale_ms * 3)
        while True:
            await asyncio.sleep(2.0)
            if (time.time() - self.last_msg_ts) * 1000.0 > threshold_ms:
                logger.warning("kalshi ws silent too long; recycling")
                await ws.close()
                return

    # ---- message handling ----

    async def _handle(self, msg: dict) -> None:
        mtype = msg.get("type")
        sid = msg.get("sid")
        seq = msg.get("seq")
        if sid is not None and seq is not None:
            last = self._seq.get(sid)
            if mtype == "orderbook_delta" and last is not None and seq != last + 1:
                raise _Reconnect(f"orderbook seq gap on sid {sid} ({last} -> {seq})")
            self._seq[sid] = seq

        body = msg.get("msg", {}) or {}
        if mtype == "subscribed":
            channel = body.get("channel", "")
            if channel and body.get("sid") is not None:
                self._channel_sids[channel] = body["sid"]
        elif mtype == "orderbook_snapshot":
            ticker = body.get("market_ticker", "")
            book = self.books.setdefault(ticker, KalshiBook(self.cfg.kalshi_use_yes_price))
            book.apply_snapshot(body)
            await self._push(("kalshi_book", ticker))
        elif mtype == "orderbook_delta":
            ticker = body.get("market_ticker", "")
            book = self.books.setdefault(ticker, KalshiBook(self.cfg.kalshi_use_yes_price))
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
        elif mtype == "cfbenchmarks_value":
            await self._push(("brti_official", body))
        elif mtype in ("market_lifecycle_v2", "multivariate_market_lifecycle"):
            await self._push(("lifecycle", body))
        elif mtype == "user_order":
            await self._push(("user_order", body))
        elif mtype == "market_position":
            await self._push(("market_position", body))
        elif mtype == "error":
            code = body.get("code")
            if code in RECONNECT_ERROR_CODES:
                raise _Reconnect(f"server error code {code}: {body.get('msg')}")
            if code == 6:  # already subscribed: harmless duplicate
                logger.debug("duplicate subscribe ignored: %s", body)
            else:
                logger.error("kalshi ws error message: %s", msg)
        # subscribed acks handled above; ok/unsubscribed/ticker need no handling.

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
        count_raw = body.get("count_fp", body.get("count", 0))
        taker = (body.get("taker_outcome_side") or body.get("taker_side") or "").lower()
        return {
            "ticker": ticker,
            "yes_price": price,
            "count": int(float(count_raw)),
            "taker_side": taker,
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


class _Reconnect(Exception):
    pass
