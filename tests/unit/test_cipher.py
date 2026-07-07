"""Foundational tests for the AES-256-GCM ``Cipher`` primitive (F4.T1).

Behaviour under test:

- Round-trip integrity across representative payload sizes.
- Key length enforcement at construction.
- Every category of authentication failure surfaces as
  :class:`InvalidCipherText` with no plaintext leakage.

The tests intentionally exercise the public surface only; the
underlying ``cryptography`` library is trusted.
"""

from __future__ import annotations

import base64
import os

import pytest

from wodbuster_worker.security.cipher import Cipher, InvalidCipherText


def _key() -> bytes:
    return os.urandom(32)


@pytest.mark.parametrize("size", [0, 1, 1024, 100 * 1024])
def test_round_trip_recovers_payload(size: int) -> None:
    cipher = Cipher(_key())
    plaintext = os.urandom(size)

    ciphertext, nonce = cipher.encrypt(plaintext)
    recovered = cipher.decrypt(ciphertext, nonce)

    assert recovered == plaintext


def test_round_trip_with_associated_data() -> None:
    cipher = Cipher(_key())
    plaintext = b"cookie=abc"
    aad = b"operator=1"

    ciphertext, nonce = cipher.encrypt(plaintext, associated_data=aad)

    assert cipher.decrypt(ciphertext, nonce, associated_data=aad) == plaintext


def test_construction_rejects_wrong_key_length() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        Cipher(os.urandom(16))


def test_construction_rejects_non_bytes_key() -> None:
    with pytest.raises(TypeError):
        Cipher("thirty-two-characters-in-a-string!!")  # type: ignore[arg-type]


def test_wrong_key_raises_invalid_ciphertext() -> None:
    plaintext = b"payload"
    ciphertext, nonce = Cipher(_key()).encrypt(plaintext)

    with pytest.raises(InvalidCipherText):
        Cipher(_key()).decrypt(ciphertext, nonce)


def test_tampered_ciphertext_raises_invalid_ciphertext() -> None:
    cipher = Cipher(_key())
    ciphertext, nonce = cipher.encrypt(b"payload")

    tampered = bytearray(ciphertext)
    tampered[0] ^= 0x01

    with pytest.raises(InvalidCipherText):
        cipher.decrypt(bytes(tampered), nonce)


def test_tampered_nonce_raises_invalid_ciphertext() -> None:
    cipher = Cipher(_key())
    ciphertext, nonce = cipher.encrypt(b"payload")

    tampered = bytearray(nonce)
    tampered[0] ^= 0x01

    with pytest.raises(InvalidCipherText):
        cipher.decrypt(ciphertext, bytes(tampered))


def test_wrong_length_nonce_raises_invalid_ciphertext() -> None:
    cipher = Cipher(_key())
    ciphertext, _ = cipher.encrypt(b"payload")

    with pytest.raises(InvalidCipherText, match="nonce length"):
        cipher.decrypt(ciphertext, os.urandom(8))


def test_mismatched_associated_data_raises_invalid_ciphertext() -> None:
    cipher = Cipher(_key())
    ciphertext, nonce = cipher.encrypt(b"payload", associated_data=b"aad-1")

    with pytest.raises(InvalidCipherText):
        cipher.decrypt(ciphertext, nonce, associated_data=b"aad-2")


def test_failure_does_not_leak_plaintext_in_exception() -> None:
    plaintext = b"super-secret-cookie-value"
    cipher = Cipher(_key())
    ciphertext, nonce = cipher.encrypt(plaintext)

    tampered = bytearray(ciphertext)
    tampered[-1] ^= 0x01

    with pytest.raises(InvalidCipherText) as excinfo:
        cipher.decrypt(bytes(tampered), nonce)

    # Belt-and-braces: the wrapper's message is opaque, and the chained
    # cryptography.InvalidTag also does not carry the plaintext.
    message = str(excinfo.value)
    assert b"super-secret" not in message.encode()
    if excinfo.value.__cause__ is not None:
        assert b"super-secret" not in str(excinfo.value.__cause__).encode()


def test_from_base64_accepts_standard_alphabet() -> None:
    # ``openssl rand -base64 32`` output uses ``+`` and ``/``. Round-trip
    # a payload through the resulting cipher to prove the alphabet was
    # decoded correctly.
    key = os.urandom(32)
    key_b64 = base64.b64encode(key).decode("ascii")
    assert "=" in key_b64  # sanity: standard alphabet with padding

    cipher = Cipher.from_base64(key_b64)
    ciphertext, nonce = cipher.encrypt(b"payload")
    assert cipher.decrypt(ciphertext, nonce) == b"payload"


def test_from_base64_accepts_urlsafe_alphabet() -> None:
    key = os.urandom(32)
    key_b64 = base64.urlsafe_b64encode(key).decode("ascii")

    cipher = Cipher.from_base64(key_b64)
    ciphertext, nonce = cipher.encrypt(b"payload")
    assert cipher.decrypt(ciphertext, nonce) == b"payload"


def test_from_base64_rejects_wrong_decoded_length() -> None:
    # 16 bytes = AES-128; we only accept AES-256. Fail loudly at
    # startup rather than at first encrypt.
    short = base64.urlsafe_b64encode(os.urandom(16)).decode("ascii")
    with pytest.raises(ValueError, match="32 bytes"):
        Cipher.from_base64(short)


def test_from_base64_rejects_non_base64_input() -> None:
    with pytest.raises(ValueError, match="valid base64"):
        Cipher.from_base64("not$%^valid&base64@@")


def test_encrypt_produces_fresh_nonce_each_call() -> None:
    cipher = Cipher(_key())
    _, nonce_a = cipher.encrypt(b"payload")
    _, nonce_b = cipher.encrypt(b"payload")

    assert nonce_a != nonce_b
