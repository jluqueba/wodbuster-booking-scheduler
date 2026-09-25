"""The period the charts describe (FR-030, slice 5).

One selector governs every chart on the page. Per-chart windows were
considered and rejected: a heatmap over a year beside a rate over a
month invites the reader to cross two figures that do not describe the
same thing, and it multiplies the interface by the number of charts.

The calendar is deliberately not governed by this. A calendar is a
month by nature, and it carries its own navigation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

PeriodKey = Literal["m1", "m3", "m12", "all", "billing"]

DEFAULT_PERIOD: PeriodKey = "m12"

# Ordered as the selector renders them. ``billing`` comes first because
# it is the window the gym itself bills on, and it is offered only when
# the gym stated its boundaries.
PERIOD_KEYS: tuple[PeriodKey, ...] = ("billing", "m1", "m3", "m12", "all")

_PERIOD_DAYS: dict[PeriodKey, int] = {
    "m1": 30,
    "m3": 91,
    "m12": 365,
}


@dataclass(frozen=True)
class Period:
    """The window the charts cover."""

    key: PeriodKey
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def resolve_period(
    requested: str | None,
    *,
    today: date,
    horizon_days: int,
    oldest_captured: date | None,
    billing: tuple[date, date] | None = None,
) -> Period:
    """Return the chart window for ``requested``.

    An unknown value falls back to the default rather than raising: a
    crafted query string is not worth a 500.

    ``all`` is bounded by what has actually been captured rather than
    by the horizon, so the label does not promise history the backfill
    has not reached yet. With nothing captured it degrades to the
    horizon, which keeps the window non-empty.

    ``billing`` is the gym's own period, available only when the points
    page stated it. Asking for it when it is unknown falls back to the
    default instead of erroring, which is FR-031 at this boundary.
    """
    key: PeriodKey = requested if requested in PERIOD_KEYS else DEFAULT_PERIOD  # type: ignore[assignment]
    if key == "billing" and billing is None:
        key = DEFAULT_PERIOD

    if key == "billing":
        assert billing is not None
        # The period runs past today while it is still open. Charts
        # describe what happened, so the window stops at today.
        start, end = billing[0], min(billing[1], today)
        floor = today - timedelta(days=horizon_days)
        return Period(key=key, start=max(start, floor), end=end)

    if key == "all":
        start = oldest_captured or today - timedelta(days=horizon_days)
    else:
        start = today - timedelta(days=_PERIOD_DAYS[key] - 1)

    # Never reach past the horizon: days older than it are not captured
    # and never will be, so including them would dilute every average
    # with a stretch of guaranteed silence.
    floor = today - timedelta(days=horizon_days)
    return Period(key=key, start=max(start, floor), end=today)


__all__ = [
    "DEFAULT_PERIOD",
    "PERIOD_KEYS",
    "Period",
    "PeriodKey",
    "resolve_period",
]
