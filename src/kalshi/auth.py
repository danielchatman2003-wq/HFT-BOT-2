"""Kalshi API request signing (RSA-PSS).

Every authenticated REST request and the websocket handshake carry:
  KALSHI-ACCESS-KEY:       API key id
  KALSHI-ACCESS-TIMESTAMP: milliseconds since epoch
  KALSHI-ACCESS-SIGNATURE: base64( RSA-PSS-SHA256( timestamp + METHOD + path ) )

The signed path INCLUDES the /trade-api/v2 (or /trade-api/ws/v2) prefix and
EXCLUDES the query string. Getting either wrong yields opaque 401s, so the
full signed path is always passed in explicitly here.
"""
from __future__ import annotations

import base64
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class KalshiSigner:
    def __init__(self, key_id: str, private_key_path: str | None = None,
                 private_key_pem: bytes | None = None) -> None:
        """Load the RSA key from a PEM file path, or directly from PEM bytes
        (used by KALSHI_PRIVATE_KEY_B64 for file-less cloud setups)."""
        self.key_id = key_id
        if private_key_pem is None:
            if not private_key_path:
                raise ValueError("provide private_key_path or private_key_pem")
            with open(private_key_path, "rb") as f:
                private_key_pem = f.read()
        self._private_key = serialization.load_pem_private_key(private_key_pem, password=None)

    def sign(self, timestamp_ms: str, method: str, signed_path: str) -> str:
        message = f"{timestamp_ms}{method}{signed_path}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def headers(self, method: str, signed_path: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self.sign(ts, method, signed_path),
        }
