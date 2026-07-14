"""Unit tests for :class:`TelegramBindStore` (US9.8).

The store guards the single-user bind flow: mint a one-shot token
from the web UI, hand it to Telegram via ``/start <token>``, look it
up in the webhook handler. These tests cover issue/consume roundtrip
and the invariants that keep a token from being reused, revealed to
another operator, or accepted after its TTL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from wodbuster_worker.notifications.telegram_bind import (
    DEFAULT_TOKEN_TTL,
    TelegramBindStore,
)


def test_issue_then_consume_returns_operator_id() -> None:
    store = TelegramBindStore()
    token = store.issue(operator_id=42)
    assert store.consume(token) == 42


def test_consume_removes_token_so_second_call_returns_none() -> None:
    store = TelegramBindStore()
    token = store.issue(operator_id=42)
    store.consume(token)
    assert store.consume(token) is None


def test_consume_unknown_token_returns_none() -> None:
    store = TelegramBindStore()
    assert store.consume("never-issued") is None


def test_consume_after_ttl_returns_none() -> None:
    store = TelegramBindStore(ttl=timedelta(minutes=10))
    issued_at = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    token = store.issue(operator_id=42, now=issued_at)
    after_ttl = issued_at + timedelta(minutes=11)
    assert store.consume(token, now=after_ttl) is None


def test_default_ttl_is_ten_minutes() -> None:
    """Small enough that a lost token stops being a leak quickly."""
    assert timedelta(minutes=10) == DEFAULT_TOKEN_TTL


def test_tokens_are_distinct_across_operators() -> None:
    store = TelegramBindStore()
    a = store.issue(operator_id=1)
    b = store.issue(operator_id=2)
    assert a != b
    assert store.consume(a) == 1
    assert store.consume(b) == 2


def test_multiple_tokens_for_same_operator_all_bind() -> None:
    """Operator generates a new link, forgets it, generates again —
    the second still works, the first stays live until consumed or
    expires."""
    store = TelegramBindStore()
    first = store.issue(operator_id=42)
    second = store.issue(operator_id=42)
    assert first != second
    assert store.consume(first) == 42
    assert store.consume(second) == 42
