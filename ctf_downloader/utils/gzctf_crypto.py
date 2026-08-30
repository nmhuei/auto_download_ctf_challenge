"""GZCTF API encryption compatible with current upstream WebCrypto client.

Wire format:
    ephemeral X25519 public key (32 bytes)
    || AES-GCM nonce (12 bytes)
    || ciphertext || 16-byte GCM tag

AES-256 key = SHA256(X25519 shared secret). The whole frame is base64 encoded.
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


class GZCTFCryptoError(ValueError):
    """Invalid/malformed GZCTF API encryption input."""


def encrypt_api_data(
    plaintext: str,
    public_key_b64: Optional[str],
    *,
    ephemeral_private_key: Optional[X25519PrivateKey] = None,
    nonce: Optional[bytes] = None,
) -> str:
    """Encrypt one API field exactly like the GZCTF WebCrypto client.

    When public_key_b64 is empty, upstream intentionally sends plaintext;
    preserve that behavior for deployments with API encryption disabled.
    Test-only key/nonce injection makes wire compatibility deterministic.
    """
    if not public_key_b64:
        return str(plaintext)

    try:
        recipient_raw = base64.b64decode(str(public_key_b64), validate=True)
    except Exception as exc:
        raise GZCTFCryptoError("GZCTF apiPublicKey không phải base64 hợp lệ") from exc
    if len(recipient_raw) != 32:
        raise GZCTFCryptoError(
            f"GZCTF apiPublicKey phải dài 32 bytes, nhận {len(recipient_raw)}"
        )

    try:
        recipient = X25519PublicKey.from_public_bytes(recipient_raw)
    except Exception as exc:
        raise GZCTFCryptoError("GZCTF apiPublicKey X25519 không hợp lệ") from exc

    ephemeral = ephemeral_private_key or X25519PrivateKey.generate()
    ephemeral_public = ephemeral.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )
    shared_secret = ephemeral.exchange(recipient)
    aes_key = hashlib.sha256(shared_secret).digest()

    iv = nonce if nonce is not None else os.urandom(12)
    if len(iv) != 12:
        raise GZCTFCryptoError("AES-GCM nonce của GZCTF phải dài 12 bytes")

    ciphertext_and_tag = AESGCM(aes_key).encrypt(
        iv, str(plaintext).encode("utf-8"), None
    )
    frame = ephemeral_public + iv + ciphertext_and_tag
    return base64.b64encode(frame).decode("ascii")
