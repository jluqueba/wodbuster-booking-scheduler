"""Unit tests for the alert-evaluator decision logic (US4.T1).

The evaluator itself calls out to :func:`compute_next_window` and
queries the ``alert`` table; both are exercised by mocking the session
at the query boundary. The "should we emit" branch matrix is the point
of this file — the persistence half is covered by
``tests/component/test_heartbeat_alerts.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from wodbuster_worker.heartbeat.alerts import (
    Clear,
    Emit,
    NoOp,
    Suppress,
    evaluate_cookie_expiring,
)

_NOW = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
_NEXT_WINDOW_SOON = _NOW + timedelta(hours=12)  # window in 12h
_NEXT_WINDOW_FAR = _NOW + timedelta(days=7)  # well beyond lead time


class _FakeSession:
    """Minimal session — the evaluator only calls ``scalar``."""

    def __init__(self, open_alert: Any = None) -> None:
        self.open_alert = open_alert
        self.scalar_calls: int = 0

    def scalar(self, _stmt: Any) -> Any:
        self.scalar_calls += 1
        return self.open_alert


class _FakeAlert:
    """Duck-typed ``Alert`` row for the evaluator's suppression check."""

    def __init__(self, acknowledged_at: datetime | None = None) -> None:
        self.acknowledged_at = acknowledged_at


def _patch_next_window(value: datetime | None):
    """Patch ``compute_next_window`` inside the evaluator module."""
    return patch(
        "wodbuster_worker.heartbeat.alerts.compute_next_window",
        return_value=value,
    )


def test_no_projection_and_no_open_alert_is_noop() -> None:
    session = _FakeSession(open_alert=None)

    with _patch_next_window(_NEXT_WINDOW_SOON):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            operator_id=1,
            projected_ttl_at=None,
            now=_NOW,
        )
    assert isinstance(result, NoOp)


def test_no_projection_but_open_alert_clears() -> None:
    session = _FakeSession(open_alert=_FakeAlert())

    with _patch_next_window(_NEXT_WINDOW_SOON):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            operator_id=1,
            projected_ttl_at=None,
            now=_NOW,
        )
    assert isinstance(result, Clear)


def test_no_next_window_returns_noop_when_no_open_alert() -> None:
    session = _FakeSession(open_alert=None)

    with _patch_next_window(None):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            operator_id=1,
            projected_ttl_at=_NOW + timedelta(days=15),
            now=_NOW,
        )
    assert isinstance(result, NoOp)


def test_no_next_window_clears_stale_open_alert() -> None:
    session = _FakeSession(open_alert=_FakeAlert())

    with _patch_next_window(None):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            operator_id=1,
            projected_ttl_at=_NOW + timedelta(days=15),
            now=_NOW,
        )
    assert isinstance(result, Clear)


def test_far_window_returns_noop_even_if_cookie_dies_before() -> None:
    # Cookie dies in 1h, but the window is 7d away. Not urgent yet;
    # the operator has plenty of time to re-paste before the 24h
    # lead-time window opens.
    session = _FakeSession(open_alert=None)
    projected = _NOW + timedelta(hours=1)

    with _patch_next_window(_NEXT_WINDOW_FAR):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            operator_id=1,
            projected_ttl_at=projected,
            now=_NOW,
        )
    assert isinstance(result, NoOp)


def test_within_lead_time_but_cookie_survives_returns_noop() -> None:
    # Window in 12h, projected TTL in 30 days => cookie survives.
    session = _FakeSession(open_alert=None)

    with _patch_next_window(_NEXT_WINDOW_SOON):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            operator_id=1,
            projected_ttl_at=_NOW + timedelta(days=30),
            now=_NOW,
        )
    assert isinstance(result, NoOp)


def test_threshold_holds_and_no_open_alert_emits() -> None:
    # Window in 12h, cookie dies in 6h => alert!
    session = _FakeSession(open_alert=None)
    projected = _NOW + timedelta(hours=6)

    with _patch_next_window(_NEXT_WINDOW_SOON):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            operator_id=1,
            projected_ttl_at=projected,
            now=_NOW,
        )
    assert isinstance(result, Emit)
    assert result.next_window_at == _NEXT_WINDOW_SOON
    assert result.projected_ttl_at == projected


def test_threshold_holds_with_open_alert_and_no_ack_re_emits() -> None:
    session = _FakeSession(open_alert=_FakeAlert(acknowledged_at=None))

    with _patch_next_window(_NEXT_WINDOW_SOON):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            operator_id=1,
            projected_ttl_at=_NOW + timedelta(hours=6),
            now=_NOW,
        )
    assert isinstance(result, Emit)


def test_threshold_holds_with_recent_ack_suppresses_this_cycle() -> None:
    # Previous heartbeat was 1h ago; acknowledgment 30min ago -> AFTER
    # the previous heartbeat -> one-cycle grace applies.
    prev_hb = _NOW - timedelta(hours=1)
    ack = _NOW - timedelta(minutes=30)
    session = _FakeSession(open_alert=_FakeAlert(acknowledged_at=ack))

    with _patch_next_window(_NEXT_WINDOW_SOON):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            operator_id=1,
            projected_ttl_at=_NOW + timedelta(hours=6),
            now=_NOW,
            previous_heartbeat_at=prev_hb,
        )
    assert isinstance(result, Suppress)


def test_threshold_holds_with_stale_ack_re_emits() -> None:
    # Previous heartbeat 1h ago; acknowledgment 2h ago -> BEFORE the
    # previous heartbeat -> grace already spent -> re-emit.
    prev_hb = _NOW - timedelta(hours=1)
    ack = _NOW - timedelta(hours=2)
    session = _FakeSession(open_alert=_FakeAlert(acknowledged_at=ack))

    with _patch_next_window(_NEXT_WINDOW_SOON):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            operator_id=1,
            projected_ttl_at=_NOW + timedelta(hours=6),
            now=_NOW,
            previous_heartbeat_at=prev_hb,
        )
    assert isinstance(result, Emit)


def test_ack_without_previous_heartbeat_re_emits() -> None:
    # Edge case: previous_heartbeat_at is None. The suppression rule
    # cannot fire without a comparison anchor; default to re-emitting.
    session = _FakeSession(open_alert=_FakeAlert(acknowledged_at=_NOW - timedelta(minutes=1)))

    with _patch_next_window(_NEXT_WINDOW_SOON):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            operator_id=1,
            projected_ttl_at=_NOW + timedelta(hours=6),
            now=_NOW,
            previous_heartbeat_at=None,
        )
    assert isinstance(result, Emit)


@pytest.mark.parametrize(
    "lead_hours,should_alert",
    [
        (1, True),  # window very soon
        (23, True),  # inside the 24h band
        (24, True),  # exactly at the boundary (<=)
        (25, False),  # just outside
        (48, False),  # far
    ],
)
def test_lead_time_boundary(lead_hours: int, should_alert: bool) -> None:
    session = _FakeSession(open_alert=None)
    next_window = _NOW + timedelta(hours=lead_hours)
    # Cookie dies before the window (10h before).
    projected = next_window - timedelta(hours=10)

    with _patch_next_window(next_window):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            operator_id=1,
            projected_ttl_at=projected,
            now=_NOW,
        )

    if should_alert:
        assert isinstance(result, Emit)
    else:
        assert isinstance(result, NoOp)
