"""Unit tests for email fan-out gating (ADR-0011)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from wodbuster_worker.notifications import fanout
from wodbuster_worker.persistence.models import OperatorProfile

_NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def _op(**kw: Any) -> OperatorProfile:
    return OperatorProfile(id=1, display_name="X", **kw)


def test_no_email_is_not_allowed() -> None:
    assert fanout.email_allowed(_op(email=None), {"kind": "booking_result"}) is False


def test_default_preferences_allow() -> None:
    assert fanout.email_allowed(_op(email="a@b"), {"kind": "booking_result"}) is True


def test_disabled_category_blocks() -> None:
    op = _op(email="a@b", email_preferences={"bookings": False})
    assert fanout.email_allowed(op, {"kind": "booking_result"}) is False


def test_other_category_unaffected() -> None:
    op = _op(email="a@b", email_preferences={"bookings": False})
    assert fanout.email_allowed(op, {"kind": "cookie_invalid"}) is True


def test_uncategorized_kind_always_allowed() -> None:
    op = _op(email="a@b", email_preferences={"bookings": False, "session_alerts": False})
    assert fanout.email_allowed(op, {"kind": "account_approved"}) is True


def test_enqueue_adds_email_row_when_allowed() -> None:
    session = _FakeSession()
    fanout.enqueue_email_row(
        session,  # type: ignore[arg-type]  # fake captures .add
        operator=_op(email="a@b"),
        gym_account_id=7,
        payload={"kind": "booking_result"},
        now=_NOW,
    )
    assert len(session.added) == 1
    row = session.added[0]
    assert row.kind == "email"
    assert row.target == "a@b"
    assert row.gym_account_id == 7


def test_enqueue_skips_when_disabled() -> None:
    session = _FakeSession()
    fanout.enqueue_email_row(
        session,  # type: ignore[arg-type]
        operator=_op(email="a@b", email_preferences={"bookings": False}),
        gym_account_id=7,
        payload={"kind": "booking_result"},
        now=_NOW,
    )
    assert session.added == []
