import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.kalshi.auth import KalshiSigner


@pytest.fixture(scope="module")
def key_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def _verify(pem: bytes, headers: dict, method: str, path: str) -> None:
    key = serialization.load_pem_private_key(pem, password=None)
    key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        f"{headers['KALSHI-ACCESS-TIMESTAMP']}{method}{path}".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_signer_from_path(tmp_path, key_pem):
    p = tmp_path / "key.pem"
    p.write_bytes(key_pem)
    signer = KalshiSigner("kid", str(p))
    headers = signer.headers("GET", "/trade-api/ws/v2")
    _verify(key_pem, headers, "GET", "/trade-api/ws/v2")


def test_signer_from_pem_bytes(key_pem):
    """The KALSHI_PRIVATE_KEY_B64 path: key supplied as bytes, no file."""
    signer = KalshiSigner("kid", private_key_pem=key_pem)
    headers = signer.headers("POST", "/trade-api/v2/portfolio/events/orders")
    assert headers["KALSHI-ACCESS-KEY"] == "kid"
    _verify(key_pem, headers, "POST", "/trade-api/v2/portfolio/events/orders")


def test_signer_requires_some_key():
    with pytest.raises(ValueError):
        KalshiSigner("kid")
