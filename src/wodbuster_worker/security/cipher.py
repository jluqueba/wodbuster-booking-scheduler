"""AES-256-GCM cipher primitive.

Wraps ``cryptography.hazmat.primitives.ciphers.aead.AESGCM`` behind a
minimal, opinionated interface. Application code owns the caller side:
this module is intentionally free of key management, key rotation, and
storage concerns.

Design choices:

- **256-bit key only.** Constructor rejects any other length. Rejecting
  invalid lengths at construction time (rather than at first
  ``encrypt``) turns a class of "cipher misconfigured" bugs into a
  loud, immediate failure at process startup, next to the Key Vault
  load.
- **96-bit random nonce per call.** NIST SP 800-38D recommends 96-bit
  nonces for GCM, and the current design generates a fresh one on
  every ``encrypt`` (no counter, no deterministic derivation). Callers
  must persist the nonce alongside the ciphertext; this module returns
  them as a ``(ciphertext, nonce)`` tuple.
- **Authenticated Associated Data (AAD) is optional.** When supplied,
  the same value must be provided on decrypt or the auth tag will
  reject the ciphertext.
- **Single exception type.** Any authentication failure (wrong key,
  tampered ciphertext, tampered nonce, tampered AAD) raises
  :class:`InvalidCipherText`. The underlying ``cryptography`` library
  never emits plaintext material on failure, and neither does this
  wrapper.
"""

from __future__ import annotations

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY_LENGTH_BYTES = 32  # 256-bit
_NONCE_LENGTH_BYTES = 12  # 96-bit, NIST SP 800-38D recommendation


class InvalidCipherText(Exception):
    """Raised when ciphertext or nonce fails GCM authentication.

    Deliberately opaque: callers should not distinguish "wrong key"
    from "tampered payload" because the distinction leaks information
    to an attacker.
    """


class Cipher:
    """Authenticated encryption with a fixed 256-bit key.

    Instances are cheap; construct one per key-loading event (typically
    once at startup after the Key Vault fetch) and reuse for the
    lifetime of the key.
    """

    __slots__ = ("_aead",)

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes):
            # AESGCM accepts bytes only. Reject early so a bytearray or
            # str at the call site fails with a clear message instead
            # of a confusing type error inside cryptography.
            raise TypeError(f"key must be bytes, got {type(key).__name__}")
        if len(key) != _KEY_LENGTH_BYTES:
            raise ValueError(f"AES-256-GCM key must be {_KEY_LENGTH_BYTES} bytes; got {len(key)}")
        self._aead = AESGCM(key)

    @classmethod
    def from_base64(cls, key_b64: str) -> Cipher:
        """Build a cipher from a base64-encoded 32-byte key.

        Accepts both standard and URL-safe base64 alphabets so a
        ``openssl rand -base64 32`` output (which uses ``+`` and ``/``)
        and a URL-safe variant (which uses ``-`` and ``_``) both work.
        The Key Vault secret is stored as a plain string; this
        constructor is what turns it into a usable ``Cipher``.

        Raises :class:`ValueError` when the decoded key is not exactly
        32 bytes, so a misconfigured secret fails loudly at startup
        rather than at first paste.
        """
        try:
            # ``urlsafe_b64decode`` accepts either alphabet as long as
            # the padding is present. If padding is missing we do not
            # silently pad: an ambiguous input is a configuration bug.
            key = base64.urlsafe_b64decode(key_b64.encode("ascii"))
        except (ValueError, binascii.Error) as exc:
            raise ValueError(f"cookie_encryption_key is not valid base64: {exc}") from exc
        return cls(key)

    def encrypt(
        self, plaintext: bytes, associated_data: bytes | None = None
    ) -> tuple[bytes, bytes]:
        """Encrypt ``plaintext``; return ``(ciphertext, nonce)``.

        The nonce is 96 random bits drawn from ``os.urandom``. Callers
        must persist both values; ciphertext without its nonce is
        unrecoverable.
        """
        nonce = os.urandom(_NONCE_LENGTH_BYTES)
        ciphertext = self._aead.encrypt(nonce, plaintext, associated_data)
        return ciphertext, nonce

    def decrypt(
        self,
        ciphertext: bytes,
        nonce: bytes,
        associated_data: bytes | None = None,
    ) -> bytes:
        """Verify and decrypt ``ciphertext``.

        Raises :class:`InvalidCipherText` on any authentication
        failure. The exception carries no diagnostic detail by
        design; log at the call site if you need one.
        """
        if len(nonce) != _NONCE_LENGTH_BYTES:
            # A wrong-length nonce cannot possibly have been produced
            # by ``encrypt``; treat it as a tampering attempt.
            raise InvalidCipherText("nonce length mismatch")
        try:
            return self._aead.decrypt(nonce, ciphertext, associated_data)
        except InvalidTag as exc:
            raise InvalidCipherText("authentication failed") from exc


__all__ = ["Cipher", "InvalidCipherText"]
