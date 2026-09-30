"""
Encrypt/decrypt helpers for external_api_keys.api_key_encrypted (the
per-meter API key CFO Platform issues for POST /external/v1/meter-readings).

NOT a one-way hash like app/auth.py's password hashing — this has to be
reversible, since the plaintext key must go back out in the X-API-Key
header on every push. Fernet (symmetric encryption, from the
`cryptography` package) is a flagged DEFAULT CHOICE — not yet confirmed
with the user specifically. Swap this module for a different scheme
(e.g. a cloud KMS) if that's wanted instead; nothing outside this file
needs to change either way, since callers only ever see plaintext in
and plaintext out.
"""
from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class EncryptionNotConfiguredError(RuntimeError):
    """
    Raised instead of silently encrypting with a blank/predictable key —
    an empty external_key_encryption_key is a configuration mistake that
    must fail loudly and immediately, not produce ciphertext that looks
    fine but is either unrecoverable or trivially crackable.
    """


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().external_key_encryption_key
    if not key:
        raise EncryptionNotConfiguredError(
            "external_key_encryption_key is not set — cannot encrypt or decrypt "
            "external API keys. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" '
            "and set it as the EXTERNAL_KEY_ENCRYPTION_KEY env var."
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as e:
        raise EncryptionNotConfiguredError(
            f"external_key_encryption_key is set but not a valid Fernet key: {e}"
        ) from e


def encrypt_api_key(plaintext: str) -> str:
    """
    Confirmed request: encrypt a CFO Platform-issued API key before it's
    stored in external_api_keys.api_key_encrypted. Returns a
    urlsafe-base64 string safe to store directly in a TEXT column.
    """
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_api_key(ciphertext: str) -> str:
    """
    Inverse of encrypt_api_key() — used right before building the
    X-API-Key header for an outgoing push. Raises ValueError (not
    InvalidToken directly) on a corrupted/tampered value or a ciphertext
    encrypted under a since-rotated external_key_encryption_key, so
    callers only need to catch one exception type.
    """
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise ValueError(
            "Could not decrypt external API key — value is corrupted, or was "
            "encrypted under a different external_key_encryption_key than the "
            "one currently configured."
        ) from e
