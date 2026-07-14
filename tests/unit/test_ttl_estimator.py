"""Unit tests for the projected-TTL estimator (US3.T5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from wodbuster_worker.heartbeat.estimator import project_ttl
from wodbuster_worker.security.cookie import Rejected, Unknown, Valid

_NOW = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
_CEILING = timedelta(days=30)


def _valid() -> Valid:
    return Valid(probed_at=_NOW)


def test_valid_with_no_previous_returns_now_plus_ceiling() -> None:
    result = project_ttl(verdict=_valid(), now=_NOW, ceiling=_CEILING, previous=None)
    assert result == _NOW + _CEILING


def test_valid_with_lower_previous_keeps_previous() -> None:
    # Yesterday's projection lands 29 days from today. Today's Valid
    # probe would suggest 30 days; ``min`` keeps the pessimistic value.
    previous = _NOW + timedelta(days=29)

    result = project_ttl(verdict=_valid(), now=_NOW, ceiling=_CEILING, previous=previous)

    assert result == previous


def test_valid_with_higher_previous_lowers_to_ceiling() -> None:
    # Defensive edge case: if a previous projection is somehow
    # further in the future than ``now + ceiling`` (should not happen
    # under normal decay, but a shortened ceiling could produce it),
    # the estimator lowers it.
    previous = _NOW + timedelta(days=60)

    result = project_ttl(verdict=_valid(), now=_NOW, ceiling=_CEILING, previous=previous)

    assert result == _NOW + _CEILING


def test_valid_probes_are_monotonic_non_increasing() -> None:
    # Simulate a two-day run of Valid probes. The projection must
    # never grow between calls: expected trajectory is +30d, +29d,
    # +28d, ... one day of decay per calendar day.
    projection: datetime | None = None
    for day in range(3):
        clock = _NOW + timedelta(days=day)
        projection = project_ttl(
            verdict=_valid(),
            now=clock,
            ceiling=_CEILING,
            previous=projection,
        )
    assert projection == _NOW + _CEILING  # +30d from _NOW, +28d from day-2 clock


def test_rejected_maps_to_immediate_expiry() -> None:
    previous = _NOW + timedelta(days=15)

    result = project_ttl(
        verdict=Rejected(reason="server said no"),
        now=_NOW,
        ceiling=_CEILING,
        previous=previous,
    )

    # Rejected forces an immediate expiry regardless of the previous
    # projection: the alert evaluator will fire on the same cycle.
    assert result == _NOW


def test_unknown_preserves_previous_projection() -> None:
    previous = _NOW + timedelta(days=10)

    result = project_ttl(
        verdict=Unknown(reason="dns blip"),
        now=_NOW,
        ceiling=_CEILING,
        previous=previous,
    )

    assert result == previous


def test_unknown_with_no_previous_stays_none() -> None:
    result = project_ttl(
        verdict=Unknown(reason="server 502"),
        now=_NOW,
        ceiling=_CEILING,
        previous=None,
    )

    # A transient failure right after a paste (before any Valid probe)
    # leaves the projection unset. The next Valid probe seeds it.
    assert result is None
