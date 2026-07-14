"""Minimal Kalshi trade API v2 client with RSA-PSS request signing.

Docs: https://trading-api.readme.io/reference/getting-started
Every authenticated request must include:
  KALSHI-ACCESS-KEY:       your API key id
  KALSHI-ACCESS-TIMESTAMP: current time in milliseconds since epoch
  KALSHI-ACCESS-SIGNATURE: base64(RSA-PSS-SHA256(timestamp + method + path))
"""
from __future__ import annotations

import base64
import time
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from src.config import CONFIG


class KalshiAPIError(RuntimeError):
    pass


class KalshiClient:
    def __init__(self, key_id: str | None = None, private_key_path: str | None = None, base_url: str | None = None):
        self.key_id = key_id or CONFIG.kalshi_api_key_id
        self.base_url = (base_url or CONFIG.kalshi_base_url).rstrip("/")
        self._private_key = None
        path = private_key_path or CONFIG.kalshi_private_key_path
        if path:
            try:
                with open(path, "rb") as f:
                    self._private_key = serialization.load_pem_private_key(f.read(), password=None)
            except FileNotFoundError:
                # Client can still be used for unauthenticated/public endpoints (market data).
                self._private_key = None

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        if self._private_key is None:
            raise KalshiAPIError(
                "No private key loaded; cannot sign authenticated requests. "
                "Set KALSHI_PRIVATE_KEY_PATH to a valid PEM file."
            )
        message = f"{timestamp_ms}{method}{path}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _headers(self, method: str, path: str) -> dict:
        timestamp_ms = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": self._sign(timestamp_ms, method, path),
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, authenticated: bool = True, **kwargs) -> Any:
        url = f"{self.base_url}{path}"
        headers = self._headers(method, path) if authenticated else {}
        resp = requests.request(method, url, headers=headers, timeout=10, **kwargs)
        if not resp.ok:
            raise KalshiAPIError(f"{method} {path} -> {resp.status_code}: {resp.text}")
        return resp.json()

    # ---- Public market data (no signing required) ----

    def get_markets(self, series_ticker: str | None = None, status: str = "open", limit: int = 100) -> list[dict]:
        params = {"status": status, "limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        data = self._request("GET", "/markets", authenticated=False, params=params)
        return data.get("markets", [])

    def get_market(self, ticker: str) -> dict:
        data = self._request("GET", f"/markets/{ticker}", authenticated=False)
        return data.get("market", {})

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        data = self._request("GET", f"/markets/{ticker}/orderbook", authenticated=False, params={"depth": depth})
        return data.get("orderbook", {})

    # ---- Authenticated account/trading endpoints ----

    def get_balance(self) -> dict:
        return self._request("GET", "/portfolio/balance")

    def get_positions(self) -> list[dict]:
        data = self._request("GET", "/portfolio/positions")
        return data.get("market_positions", [])

    def place_order(
        self,
        ticker: str,
        side: str,  # "yes" or "no"
        action: str,  # "buy" or "sell"
        count: int,
        price_cents: int,
        client_order_id: str,
        order_type: str = "limit",
    ) -> dict:
        body = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
            "client_order_id": client_order_id,
        }
        price_key = "yes_price" if side == "yes" else "no_price"
        body[price_key] = price_cents
        return self._request("POST", "/portfolio/orders", json=body)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/portfolio/orders/{order_id}")
