"""Async Kalshi trade API v2 REST client with request signing and
client-side token-bucket rate limiting.

Rate limits: Kalshi meters independent read/write token buckets (basic tier
200 read / 100 write tokens per second, default 10 tokens per request).
The client enforces the same shape locally so a busy strategy queues
instead of drawing 429s.

Order placement supports two wire flavors (KALSHI_ORDER_API):
  v2     -> POST /portfolio/events/orders, unified YES-book: side bid/ask,
            decimal-string dollar price. (Current docs.kalshi.com style.)
  legacy -> POST /portfolio/orders, side yes/no + action buy/sell +
            integer-cent prices. (Scheduled for deprecation.)
docs.kalshi.com was not reachable from the environment this was written in,
so field names for the v2 flavor follow Kalshi's published quick-start
examples -- run against the demo environment first and flip the flavor if
order placement 4xx's.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from src.config import Config
from src.kalshi.auth import KalshiSigner

logger = logging.getLogger("kalshi.rest")

API_PREFIX = "/trade-api/v2"


class KalshiAPIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = message


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: float | None = None) -> None:
        self.rate = rate_per_sec
        self.capacity = capacity if capacity is not None else rate_per_sec
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= cost:
                    self.tokens -= cost
                    return
                await asyncio.sleep((cost - self.tokens) / self.rate)


class KalshiRest:
    def __init__(self, cfg: Config, signer: KalshiSigner | None) -> None:
        self.cfg = cfg
        self.signer = signer
        self.host = cfg.kalshi_host
        self._session: aiohttp.ClientSession | None = None
        self._read_bucket = TokenBucket(cfg.read_tokens_per_sec)
        self._write_bucket = TokenBucket(cfg.write_tokens_per_sec)

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        auth: bool = True,
        retries: int = 3,
    ) -> Any:
        signed_path = f"{API_PREFIX}{path}"
        url = f"{self.host}{signed_path}"
        bucket = self._read_bucket if method == "GET" else self._write_bucket
        await bucket.acquire(self.cfg.tokens_per_request)

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if auth:
            if self.signer is None:
                raise KalshiAPIError(0, "authenticated endpoint called without API credentials")
            headers.update(self.signer.headers(method, signed_path))

        session = await self.session()
        backoff = 0.5
        for attempt in range(retries + 1):
            try:
                async with session.request(method, url, params=params, json=json_body, headers=headers) as resp:
                    if resp.status in (429, 502, 503, 504) and attempt < retries:
                        logger.warning("%s %s -> %s, retrying in %.1fs", method, path, resp.status, backoff)
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        if auth and self.signer is not None:
                            headers.update(self.signer.headers(method, signed_path))
                        continue
                    if resp.status >= 400:
                        raise KalshiAPIError(resp.status, await resp.text())
                    if resp.status == 204:
                        return {}
                    return await resp.json()
            except aiohttp.ClientError as exc:
                if attempt < retries:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise KalshiAPIError(0, f"network error: {exc}") from exc
        raise KalshiAPIError(0, "unreachable")

    # ---- public market data ----

    async def get_markets(self, series_ticker: str | None = None, status: str = "open", limit: int = 200) -> list[dict]:
        markets: list[dict] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": min(limit, 200)}
            if series_ticker:
                params["series_ticker"] = series_ticker
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            data = await self._request("GET", "/markets", params=params, auth=False)
            markets.extend(data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor or len(markets) >= limit:
                break
        return markets

    async def get_market(self, ticker: str) -> dict:
        data = await self._request("GET", f"/markets/{ticker}", auth=False)
        return data.get("market", {})

    async def get_orderbook(self, ticker: str, depth: int = 32) -> dict:
        data = await self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth}, auth=False)
        return data.get("orderbook", {})

    # ---- portfolio ----

    async def get_balance(self) -> dict:
        return await self._request("GET", "/portfolio/balance")

    async def get_positions(self) -> list[dict]:
        data = await self._request("GET", "/portfolio/positions")
        return data.get("market_positions", [])

    async def get_resting_orders(self, ticker: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"status": "resting"}
        if ticker:
            params["ticker"] = ticker
        data = await self._request("GET", "/portfolio/orders", params=params)
        return data.get("orders", [])

    async def get_fills(self, ticker: str | None = None, limit: int = 100) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        data = await self._request("GET", "/portfolio/fills", params=params)
        return data.get("fills", [])

    # ---- orders ----

    async def create_order(
        self,
        ticker: str,
        side_intent: str,  # "buy_yes" | "sell_yes"
        price: float,      # YES-dollars
        count: int,
        tif: str = "gtc",  # "gtc" | "ioc"
        post_only: bool = False,
        expire_in_s: float | None = None,
        client_order_id: str | None = None,
        closing: bool = False,  # legacy flavor: True -> explicit sell of held YES
    ) -> dict:
        if side_intent not in ("buy_yes", "sell_yes"):
            raise ValueError(f"bad side_intent {side_intent!r}")
        if count <= 0:
            raise ValueError("count must be positive")
        if not (0.0 < price < 1.0):
            raise ValueError(f"price {price} outside (0, 1)")
        coid = client_order_id or str(uuid.uuid4())

        if self.cfg.kalshi_order_api == "legacy":
            body = self._legacy_order_body(ticker, side_intent, price, count, tif, expire_in_s, coid, closing)
            data = await self._request("POST", "/portfolio/orders", json_body=body)
        else:
            body = self._v2_order_body(ticker, side_intent, price, count, tif, post_only, expire_in_s, coid)
            data = await self._request("POST", "/portfolio/events/orders", json_body=body)
        return data.get("order", data)

    def _v2_order_body(
        self, ticker: str, side_intent: str, price: float, count: int,
        tif: str, post_only: bool, expire_in_s: float | None, coid: str,
    ) -> dict:
        body: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": coid,
            "side": "bid" if side_intent == "buy_yes" else "ask",
            "count": str(count),
            "price": f"{price:.4f}",
            "time_in_force": "immediate_or_cancel" if tif == "ioc" else "good_till_canceled",
        }
        if post_only:
            body["post_only"] = True
        if tif != "ioc" and expire_in_s:
            expires = datetime.now(timezone.utc) + timedelta(seconds=expire_in_s)
            body["expiration_time"] = expires.isoformat(timespec="seconds").replace("+00:00", "Z")
        return body

    def _legacy_order_body(
        self, ticker: str, side_intent: str, price: float, count: int,
        tif: str, expire_in_s: float | None, coid: str, closing: bool,
    ) -> dict:
        yes_cents = int(round(price * 100))
        yes_cents = min(99, max(1, yes_cents))
        body: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": coid,
            "type": "limit",
            "count": count,
        }
        if side_intent == "buy_yes":
            body.update(action="buy", side="yes", yes_price=yes_cents)
        elif closing:
            body.update(action="sell", side="yes", yes_price=yes_cents)
        else:
            # Opening short-YES exposure on the legacy API = buying NO.
            body.update(action="buy", side="no", no_price=100 - yes_cents)
        if tif == "ioc":
            # Legacy API has no IOC flag; a 1-second expiry is the closest thing.
            body["expiration_ts"] = int(time.time()) + 1
        elif expire_in_s:
            body["expiration_ts"] = int(time.time() + expire_in_s)
        return body

    async def cancel_order(self, order_id: str) -> dict:
        if self.cfg.kalshi_order_api == "legacy":
            return await self._request("DELETE", f"/portfolio/orders/{order_id}")
        return await self._request("DELETE", f"/portfolio/events/orders/{order_id}")

    async def batch_cancel(self, order_ids: list[str]) -> None:
        if not order_ids:
            return
        if self.cfg.kalshi_order_api != "legacy":
            try:
                await self._request(
                    "DELETE", "/portfolio/events/orders/batched", json_body={"order_ids": order_ids}
                )
                return
            except KalshiAPIError as exc:
                logger.warning("batch cancel failed (%s); falling back to per-order cancels", exc)
        for oid in order_ids:
            try:
                await self.cancel_order(oid)
            except KalshiAPIError as exc:
                # 404 = already gone (filled or expired); that's fine.
                if exc.status != 404:
                    logger.warning("cancel %s failed: %s", oid, exc)
