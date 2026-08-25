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

    def __init__(
        self,
        last_emitted_at: datetime = _NOW,
        acknowledged_at: datetime | None = None,
    ) -> None:
        self.last_emitted_at = last_emitted_at
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
            gym_account_id=1,
            projected_ttl_at=None,
            now=_NOW,
        )
    assert isinstance(result, NoOp)


def test_no_projection_but_open_alert_clears() -> None:
    session = _FakeSession(open_alert=_FakeAlert())

    with _patch_next_window(_NEXT_WINDOW_SOON):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            gym_account_id=1,
            projected_ttl_at=None,
            now=_NOW,
        )
    assert isinstance(result, Clear)


def test_no_next_window_returns_noop_when_no_open_alert() -> None:
    session = _FakeSession(open_alert=None)

    with _patch_next_window(None):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            gym_account_id=1,
            projected_ttl_at=_NOW + timedelta(days=15),
            now=_NOW,
        )
    assert isinstance(result, NoOp)


def test_no_next_window_clears_stale_open_alert() -> None:
    session = _FakeSession(open_alert=_FakeAlert())

    with _patch_next_window(None):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            gym_account_id=1,
            projected_ttl_at=_NOW + timedelta(days=15),
            now=_NOW,
        )
    assert isinstance(result, Clear)


def test_far_window_returns_noop_even_if_cookie_dies_before() -> None:
    # Cookie dies in 1h, but the window is 7d away. Not urgent yet;
    # the operator has plenty of time to re-paste before the 72h
    # lead-time window opens.
    session = _FakeSession(open_alert=None)
    projected = _NOW + timedelta(hours=1)

    with _patch_next_window(_NEXT_WINDOW_FAR):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            gym_account_id=1,
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
            gym_account_id=1,
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
            gym_account_id=1,
            projected_ttl_at=projected,
            now=_NOW,
        )
    assert isinstance(result, Emit)
    assert result.next_window_at == _NEXT_WINDOW_SOON
    assert result.projected_ttl_at == projected


def test_threshold_holds_with_open_alert_past_refire_interval_re_emits() -> None:
    # Last emitted 9h ago, past the 8h re-fire interval -> re-emit.
    session = _FakeSession(
        open_alert=_FakeAlert(last_emitted_at=_NOW - timedelta(hours=9), acknowledged_at=None)
    )

    with _patch_next_window(_NEXT_WINDOW_SOON):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            gym_account_id=1,
            projected_ttl_at=_NOW + timedelta(hours=6),
            now=_NOW,
        )
    assert isinstance(result, Emit)


def test_open_alert_inside_refire_window_suppresses() -> None:
    # Last emitted 3h ago, condition still holds -> suppress (CC-002).
    session = _FakeSession(open_alert=_FakeAlert(last_emitted_at=_NOW - timedelta(hours=3)))

    with _patch_next_window(_NEXT_WINDOW_SOON):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            gym_account_id=1,
            projected_ttl_at=_NOW + timedelta(hours=6),
            now=_NOW,
        )
    assert isinstance(result, Suppress)


def test_open_alert_past_refire_interval_re_emits() -> None:
    # Last emitted 8h 1m ago, condition still holds -> re-emit (CC-003).
    session = _FakeSession(
        open_alert=_FakeAlert(last_emitted_at=_NOW - timedelta(hours=8, minutes=1))
    )

    with _patch_next_window(_NEXT_WINDOW_SOON):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            gym_account_id=1,
            projected_ttl_at=_NOW + timedelta(hours=6),
            now=_NOW,
        )
    assert isinstance(result, Emit)


def test_open_alert_exactly_at_refire_boundary_re_emits() -> None:
    # Exact 8h boundary re-emits (">=" gate, "never earlier" edge case).
    session = _FakeSession(open_alert=_FakeAlert(last_emitted_at=_NOW - timedelta(hours=8)))

    with _patch_next_window(_NEXT_WINDOW_SOON):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            gym_account_id=1,
            projected_ttl_at=_NOW + timedelta(hours=6),
            now=_NOW,
        )
    assert isinstance(result, Emit)


def test_ack_has_no_effect_on_refire_timing() -> None:
    # Acknowledged right after emission; at 8h + 1m the alert still
    # re-emits — acknowledgement never extends the re-fire window
    # (INV-001, FR-007).
    last_emitted_at = _NOW - timedelta(hours=8, minutes=1)
    session = _FakeSession(
        open_alert=_FakeAlert(
            last_emitted_at=last_emitted_at,
            acknowledged_at=last_emitted_at + timedelta(minutes=1),
        )
    )

    with _patch_next_window(_NEXT_WINDOW_SOON):
        result = evaluate_cookie_expiring(
            session=session,  # type: ignore[arg-type]
            gym_account_id=1,
            projected_ttl_at=_NOW + timedelta(hours=6),
            now=_NOW,
        )
    assert isinstance(result, Emit)


@pytest.mark.parametrize(
    "lead_hours,should_alert",
    [
        (1, True),  # window very soon
        (71, True),  # inside the 72h band
        (72, True),  # exactly at the boundary (<=)
        (73, False),  # just outside
        (96, False),  # far
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
            gym_account_id=1,
            projected_ttl_at=projected,
            now=_NOW,
        )

    if should_alert:
        assert isinstance(result, Emit)
    else:
        assert isinstance(result, NoOp)
