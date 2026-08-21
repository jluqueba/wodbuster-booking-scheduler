"""Unit tests for signed unsubscribe tokens (ADR-0011)."""

from __future__ import annotations

from wodbuster_worker.notifications.unsubscribe import (
    make_unsubscribe_token,
    read_unsubscribe_token,
)

_SECRET = "unit-secret"


def test_roundtrip() -> None:
    token = make_unsubscribe_token(42, secret=_SECRET)
    assert read_unsubscribe_token(token, secret=_SECRET) == 42


def test_wrong_secret_rejected() -> None:
    token = make_unsubscribe_token(42, secret="a")
    assert read_unsubscribe_token(token, secret="b") is None


def test_tampered_token_rejected() -> None:
    token = make_unsubscribe_token(42, secret=_SECRET)
    assert read_unsubscribe_token(token[:-3] + "xyz", secret=_SECRET) is None


def test_expired_token_rejected() -> None:
    token = make_unsubscribe_token(42, secret=_SECRET)
    assert read_unsubscribe_token(token, secret=_SECRET, max_age_s=-1) is None
